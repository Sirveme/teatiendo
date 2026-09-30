"""Presets por rubro: SOLO datos. Un único motor (app/crm.py) los aplica copiando etapas y campos a las
tablas del tenant, que después se pueden renombrar y ampliar desde Ajustes sin tocar código.

avanzable_por_ia: la IA puede mover un contacto a esa etapa (solo hacia adelante). Las etapas con riesgo
(pagos, comprobantes, atenciones realizadas) y "Perdido" las marca siempre una persona o un módulo futuro.
"""

MODULOS = ("pedidos", "pagos", "comprobantes", "agenda")
ETIQUETAS_MODULO = {"pedidos": "Pedidos", "pagos": "Pagos", "comprobantes": "Comprobantes", "agenda": "Agenda"}
TIPOS_CAMPO = ("texto", "numero", "moneda", "opcion", "documento")
ETIQUETAS_TIPO = {"texto": "Texto", "numero": "Número", "moneda": "Monto (S/)", "opcion": "Opción de una lista",
                  "documento": "DNI o RUC"}

PRESETS = {
    "comercio_redes": {
        "nombre": "Comercio en redes",
        "descripcion": "Vendedores de Instagram, TikTok o Facebook que cobran por Yape o Plin y emiten comprobante.",
        "etapas": [
            {"clave": "consulta", "nombre": "Consulta", "color": "#8FB8DE", "avanzable_por_ia": True,
             "descripcion_ia": "El cliente pregunta por un producto, precio, stock o envío."},
            {"clave": "cotizado", "nombre": "Cotizado", "color": "#C9A0DC", "avanzable_por_ia": True,
             "descripcion_ia": "El negocio ya le indicó precio o condiciones de un producto concreto."},
            {"clave": "pedido_confirmado", "nombre": "Pedido confirmado", "color": "#F2C66D", "avanzable_por_ia": True,
             "descripcion_ia": "El cliente confirmó que quiere comprar: producto, cantidad y forma de entrega acordados."},
            {"clave": "pagado", "nombre": "Pagado", "color": "#8FD6A8", "avanzable_por_ia": False,
             "descripcion_ia": "Pago verificado por el negocio."},
            {"clave": "comprobante_enviado", "nombre": "Comprobante enviado", "color": "#7FD1C7", "avanzable_por_ia": False,
             "descripcion_ia": "Se emitió y envió la boleta o factura."},
            {"clave": "entregado", "nombre": "Entregado", "color": "#D4A017", "avanzable_por_ia": False,
             "descripcion_ia": "El pedido fue entregado."},
            {"clave": "perdido", "nombre": "Perdido", "color": "#FF9E8C", "avanzable_por_ia": False, "es_perdido": True,
             "descripcion_ia": "Venta perdida."},
        ],
        "campos": [
            {"clave": "producto", "etiqueta": "Producto y variante", "tipo": "texto",
             "descripcion_ia": "Producto que el cliente quiere, con su variante (talla, color, modelo)."},
            {"clave": "cantidad", "etiqueta": "Cantidad", "tipo": "numero",
             "descripcion_ia": "Cantidad de unidades que el cliente quiere."},
            {"clave": "tipo_comprobante", "etiqueta": "Tipo de comprobante", "tipo": "opcion", "opciones": ["Boleta", "Factura"],
             "descripcion_ia": "Comprobante que pidió el cliente: boleta o factura."},
            {"clave": "documento", "etiqueta": "DNI o RUC del comprador", "tipo": "documento",
             "descripcion_ia": "DNI (8 dígitos) para boleta o RUC (11 dígitos) para factura, tal como lo escribió el cliente."},
            {"clave": "entrega", "etiqueta": "Entrega", "tipo": "opcion", "opciones": ["Delivery", "Recojo"],
             "descripcion_ia": "Si el cliente quiere delivery o recoger el pedido."},
            {"clave": "direccion", "etiqueta": "Dirección de entrega", "tipo": "texto",
             "descripcion_ia": "Dirección de entrega, solo si es delivery."},
            {"clave": "monto", "etiqueta": "Monto", "tipo": "moneda",
             "descripcion_ia": "Monto total acordado en soles, solo si se mencionó explícitamente en la conversación."},
            {"clave": "medio_pago", "etiqueta": "Medio de pago", "tipo": "opcion",
             "opciones": ["Yape", "Plin", "Transferencia", "Efectivo"],
             "descripcion_ia": "Medio de pago que el cliente eligió o mencionó. No significa que ya pagó."},
        ],
        "motivos_perdida": ["Precio", "Sin stock", "No respondió", "Compró en otro lado", "Otro"],
        "modulos": {"pedidos": True, "pagos": True, "comprobantes": True, "agenda": False},
    },
    "clinica": {
        "nombre": "Clínica o consultorio",
        "descripcion": "Consultas, citas y atenciones de pacientes.",
        "etapas": [
            {"clave": "consulta", "nombre": "Consulta", "color": "#8FB8DE", "avanzable_por_ia": True,
             "descripcion_ia": "El paciente pregunta por especialidades, precios u horarios."},
            {"clave": "cita_solicitada", "nombre": "Cita solicitada", "color": "#F2C66D", "avanzable_por_ia": True,
             "descripcion_ia": "El paciente pidió una cita para una especialidad o fecha."},
            {"clave": "cita_confirmada", "nombre": "Cita confirmada", "color": "#8FD6A8", "avanzable_por_ia": False,
             "descripcion_ia": "El equipo confirmó la cita."},
            {"clave": "atendido", "nombre": "Atendido", "color": "#D4A017", "avanzable_por_ia": False,
             "descripcion_ia": "El paciente fue atendido."},
            {"clave": "perdido", "nombre": "Perdido", "color": "#FF9E8C", "avanzable_por_ia": False, "es_perdido": True,
             "descripcion_ia": "El paciente no concretó la cita."},
        ],
        "campos": [
            {"clave": "especialidad", "etiqueta": "Especialidad", "tipo": "texto",
             "descripcion_ia": "Especialidad o servicio que busca el paciente."},
            {"clave": "fecha_preferida", "etiqueta": "Fecha preferida", "tipo": "texto",
             "descripcion_ia": "Día u horario que el paciente prefiere para su cita."},
            {"clave": "documento", "etiqueta": "DNI del paciente", "tipo": "documento",
             "descripcion_ia": "DNI del paciente, solo si lo escribió."},
        ],
        "motivos_perdida": ["No respondió", "Precio", "Sin disponibilidad", "Se atendió en otro lugar", "Otro"],
        "modulos": {"pedidos": False, "pagos": True, "comprobantes": True, "agenda": True},
    },
    "generico": {
        "nombre": "Genérico",
        "descripcion": "Embudo simple para cualquier negocio.",
        "etapas": [
            {"clave": "nuevo", "nombre": "Nuevo", "color": "#8FB8DE", "avanzable_por_ia": True,
             "descripcion_ia": "Primer contacto del cliente."},
            {"clave": "interesado", "nombre": "Interesado", "color": "#F2C66D", "avanzable_por_ia": True,
             "descripcion_ia": "El cliente mostró interés concreto en un producto o servicio."},
            {"clave": "cliente", "nombre": "Cliente", "color": "#8FD6A8", "avanzable_por_ia": False,
             "descripcion_ia": "Concretó la compra o contrató el servicio."},
            {"clave": "perdido", "nombre": "Perdido", "color": "#FF9E8C", "avanzable_por_ia": False, "es_perdido": True,
             "descripcion_ia": "No concretó."},
        ],
        "campos": [
            {"clave": "interes", "etiqueta": "Interés", "tipo": "texto",
             "descripcion_ia": "Producto o servicio que le interesa al cliente."},
        ],
        "motivos_perdida": ["Precio", "No respondió", "Otro"],
        "modulos": {"pedidos": False, "pagos": False, "comprobantes": False, "agenda": False},
    },
}
