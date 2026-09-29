# Te Atiendo

Plataforma de atención y ventas por WhatsApp sobre la **API oficial de Meta (Cloud API)**.
Desarrollado por **PERU SISTEMAS PRO E.I.R.L.**

Esta versión es el mínimo funcional para el App Review de Meta:

- **Video 1:** enviar un mensaje desde la app y recibirlo en WhatsApp.
- **Video 2:** crear una plantilla de mensaje desde la app.

Stack: FastAPI · PostgreSQL (asyncpg) · Jinja2 + HTMX · Vanilla JS · Railway.

---

## 1. Variables de entorno

Todas son obligatorias. Si falta alguna o tiene un formato inválido, la app **no arranca** y muestra en los logs cuál es.

| Variable | Qué es | Dónde se obtiene |
|---|---|---|
| `DATABASE_URL` | Conexión a PostgreSQL | Railway: en el servicio web, `${{Postgres.DATABASE_URL}}` (variable de referencia) |
| `WA_ACCESS_TOKEN` | Token de acceso de la Graph API | Meta for Developers → tu app → WhatsApp → **Configuración de la API** (token temporal de 24 h, útil para probar). Para producción: Business Manager → Usuarios del sistema → Generar token, con los permisos `whatsapp_business_messaging` y `whatsapp_business_management` |
| `WA_PHONE_NUMBER_ID` | Identificador del número de teléfono (no es el número) | WhatsApp → **Configuración de la API** → «Identificador de número de teléfono» |
| `WA_WABA_ID` | Identificador de la cuenta de WhatsApp Business | WhatsApp → **Configuración de la API** → «Identificador de la cuenta de WhatsApp Business» |
| `WA_VERIFY_TOKEN` | Frase secreta que **tú inventas** para verificar el webhook | La misma que escribirás en el panel de Meta al configurar el webhook |
| `META_APP_SECRET` | Clave secreta de la app (se usa para validar la firma `X-Hub-Signature-256`) | Meta for Developers → tu app → Configuración de la app → **Básica** → Clave secreta de la app |
| `GRAPH_API_VERSION` | Versión de la Graph API, formato `vNN.N` | Se toma del **ejemplo curl de la página «Configuración de la API»** del panel de Meta. En la URL `https://graph.facebook.com/v23.0/…` la versión es `v23.0` |
| `SESSION_SECRET` | Clave para firmar la cookie de sesión (mínimo 32 caracteres) | La genera `python generar_hash.py` |
| `ADMIN_EMAIL` | Correo del administrador del panel | El que tú elijas |
| `ADMIN_PASSWORD_HASH` | Hash Argon2id de la contraseña del administrador | La genera `python generar_hash.py` |

### Generar la contraseña del administrador

```bash
pip install argon2-cffi
python generar_hash.py
```

El script pide la contraseña dos veces (mínimo 10 caracteres) e imprime `ADMIN_PASSWORD_HASH` y un `SESSION_SECRET` aleatorio. Pega los valores **tal cual** en Railway, con los `$` incluidos y sin comillas.

---

## 2. Deploy en Railway

1. **Sube el código a GitHub.** Crea un repositorio y arrastra los archivos de la lista del final (respeta las carpetas `app/`, `app/templates/` y `app/static/`).
2. **Crea el proyecto en Railway:** New Project → *Deploy from GitHub repo* → elige el repositorio.
3. **Agrega la base de datos:** en el mismo proyecto → *+ Create* → *Database* → **PostgreSQL**.
4. **Crea las tablas:**
   - En el servicio Postgres → pestaña *Connect* → *Public Network*: copia host, puerto, usuario, contraseña y base de datos.
   - En PGAdmin: *Register → Server* con esos datos. Luego abre *Query Tool*, pega el contenido de `schema.sql` y ejecútalo (F5).
   - El script es idempotente: puedes volver a ejecutarlo sin perder datos.
5. **Carga las variables:** en el servicio web → pestaña *Variables* → agrega las 10 variables de la tabla.
   - En `DATABASE_URL` escribe exactamente `${{Postgres.DATABASE_URL}}` (Railway la resuelve a la URL interna).
6. **Comando de inicio:** Railway lo lee del `Procfile`. Si no lo detecta, pégalo en *Settings → Deploy → Custom Start Command*:
   ```
   uvicorn app.main:app --host 0.0.0.0 --port $PORT --proxy-headers --forwarded-allow-ips "*"
   ```
7. **Dominio público:** *Settings → Networking → Generate Domain*. Obtendrás algo como `https://teatiendo-production.up.railway.app`.
8. **Healthcheck (opcional):** *Settings → Deploy → Healthcheck Path* = `/salud`.
9. **Verifica el arranque:** en *Deployments → View logs* debe aparecer `Te Atiendo listo · tenant 1 · número …`.
   - Si falta una variable o no se ejecutó `schema.sql`, el log lo dice explícitamente.

El tenant inicial (**PERU SISTEMAS PRO E.I.R.L.**) se crea solo al arrancar, con `WA_WABA_ID` y `WA_PHONE_NUMBER_ID`.

La versión de Python queda fijada en 3.12 por el archivo `.python-version`.

---

## 3. Configurar el webhook en Meta

1. Meta for Developers → tu app → **WhatsApp → Configuración** (*Configuration*).
2. En **Webhook** → *Editar*:
   - **URL de devolución de llamada:** `https://TU-DOMINIO.up.railway.app/webhook`
   - **Token de verificación:** el mismo valor de `WA_VERIFY_TOKEN`
   - Pulsa **Verificar y guardar**. Meta hace un GET a `/webhook` y la app responde con el `hub.challenge`.
3. En **Campos del webhook** → busca **`messages`** → **Suscribirse**.
4. **Suscribe la app a tu WABA.** Este paso se olvida seguido y sin él no llegan los eventos del número real. Ejecútalo una sola vez:
   ```bash
   curl -X POST "https://graph.facebook.com/v23.0/TU_WABA_ID/subscribed_apps" \
        -H "Authorization: Bearer TU_WA_ACCESS_TOKEN"
   ```
   Debe responder `{"success":true}`.
5. **Prueba:** escribe desde tu WhatsApp al número de la empresa. El contacto y el mensaje deben aparecer en la **Bandeja** en unos segundos.
   - Cada evento queda guardado sin procesar en la tabla `webhook_events`.

**Seguridad:** todo POST a `/webhook` se valida con HMAC-SHA256 del cuerpo crudo usando `META_APP_SECRET`. Si la firma no coincide, la app responde 403 y no guarda nada.

---

## 4. Número de prueba y modo desarrollo

> **Importante:** mientras la app esté en **modo desarrollo** o uses el número de prueba que da Meta, solo puedes enviar mensajes a los números que agregues en la lista de destinatarios permitidos:
> WhatsApp → **Configuración de la API** → campo **«Para»** → *Administrar lista de números de teléfono* (máximo 5). Meta envía un código de verificación por WhatsApp a cada número que agregues.
>
> Si intentas enviar a un número que no está en la lista, Meta responde el **error 131030**. La app lo muestra en la burbuja del mensaje con una explicación en español.

---

## 5. Cómo funciona

- **Regla de 24 horas.** El texto libre solo se permite si el cliente escribió en las últimas 24 horas. Se valida en el servidor y la interfaz muestra el tiempo restante. Con la ventana cerrada, la caja de texto se reemplaza por el selector de plantillas **aprobadas**.
- **Estados de entrega.** ✓ enviado, ✓✓ entregado, ✓✓ dorado leído, ! fallido (con el error de Meta legible). Se actualizan con los *statuses* del webhook y nunca retroceden.
- **Idempotencia.** `messages.wamid` es único: si Meta reenvía el mismo evento, no se duplica.
- **Plantillas.** Solo se crean en español (`es`), con categoría Utilidad o Marketing. Las variables `{{1}}`, `{{2}}`… exigen un ejemplo cada una, porque Meta lo pide para la revisión.
  - Al abrir la página se sincroniza con Meta, y mientras haya plantillas en revisión se vuelve a sincronizar cada 30 segundos.
  - Si una plantilla es rechazada, se muestra el motivo.
- **Refresco.** La conversación abierta se actualiza cada 5 s y la lista de contactos cada 10 s (HTMX 2.0.4).

---

## 6. Guion sugerido para los videos del App Review

**Video 1: envío de mensaje**
1. Muestra el login y entra al panel.
2. Ve a **Nuevo mensaje**, escribe tu número (debe estar en la lista permitida), elige una plantilla aprobada (por ejemplo `hello_world`) y envía.
3. Enseña el celular: llega el mensaje. En el panel el estado pasa de ✓ a ✓✓.
4. Responde desde el celular. El mensaje aparece en la bandeja y la insignia cambia a «Ventana abierta».
5. Escribe una respuesta de texto libre desde el panel y muestra que llega al celular.

**Video 2: creación de plantilla**
1. Ve a **Plantillas** y completa nombre (`confirmacion_pedido`), categoría y un cuerpo con `{{1}}`.
2. Escribe el ejemplo y enseña la vista previa. Pulsa **Enviar a revisión de Meta**.
3. La plantilla aparece como «En revisión · PENDING».
4. Pulsa **Sincronizar con Meta** para mostrar el estado actualizado. Si quieres, muéstrala también en el Administrador de WhatsApp de Meta.

---

## 7. Prueba local (opcional)

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
copy .env.example .env          # y completa los valores (usa comillas simples)
uvicorn app.main:app --reload --env-file .env
```

Abre `http://localhost:8000`. En localhost la cookie de sesión funciona sin HTTPS; en Railway siempre es `Secure` (https_only). Para recibir webhooks en local necesitas un túnel HTTPS (por ejemplo, ngrok).
