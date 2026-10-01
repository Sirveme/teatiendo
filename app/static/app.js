/* Te Atiendo · interacciones del panel (Vanilla JS + HTMX) */
(function () {
  'use strict';

  var RE_VAR = /\{\{\s*(\d+)\s*\}\}/g;

  function buscar(raiz, selector) {
    var lista = Array.prototype.slice.call(raiz.querySelectorAll ? raiz.querySelectorAll(selector) : []);
    if (raiz.matches && raiz.matches(selector)) lista.unshift(raiz);
    return lista;
  }

  function numerosVariables(texto) {
    var vistos = {};
    (texto.match(RE_VAR) || []).forEach(function (t) { vistos[+t.replace(/\D/g, '')] = true; });
    return Object.keys(vistos).map(Number).sort(function (a, b) { return a - b; });
  }

  function leerValores(el) {
    try { return JSON.parse(el.getAttribute('data-valores') || '[]') || []; } catch (e) { return []; }
  }

  /* Pinta el cuerpo reemplazando {{n}} con los valores; lo que falta queda resaltado. Sin innerHTML. */
  function pintarVistaPrevia(destino, cuerpo, valores) {
    destino.textContent = '';
    if (!cuerpo) {
      destino.textContent = destino.getAttribute('data-vacio') || '';
      destino.classList.toggle('vacia', !!destino.textContent);
      return;
    }
    destino.classList.remove('vacia');
    var ultimo = 0;
    cuerpo.replace(RE_VAR, function (token, n, pos) {
      destino.appendChild(document.createTextNode(cuerpo.slice(ultimo, pos)));
      var span = document.createElement('span');
      span.className = 'var';
      span.textContent = valores[n - 1] || token;
      destino.appendChild(span);
      ultimo = pos + token.length;
      return token;
    });
    destino.appendChild(document.createTextNode(cuerpo.slice(ultimo)));
  }

  /* Crea n campos <prefijo>_1..n conservando lo ya escrito. */
  function construirCampos(contenedor, n, prefijo, etiqueta, previos) {
    var actuales = buscar(contenedor, 'input').map(function (i) { return i.value; });
    if (actuales.length === n && n > 0) return;
    contenedor.textContent = '';
    for (var i = 1; i <= n; i++) {
      var label = document.createElement('label');
      label.className = 'campo-variable';
      label.appendChild(document.createTextNode(etiqueta + ' '));
      var code = document.createElement('code');
      code.textContent = '{{' + i + '}}';
      label.appendChild(code);
      var input = document.createElement('input');
      input.name = prefijo + '_' + i;
      input.required = true;
      input.maxLength = 1024;
      input.autocomplete = 'off';
      input.value = actuales[i - 1] != null ? actuales[i - 1] : (previos[i - 1] || '');
      label.appendChild(input);
      contenedor.appendChild(label);
    }
  }

  function valoresDe(contenedor) {
    return buscar(contenedor, 'input').map(function (i) { return i.value.trim(); });
  }

  /* --- Selector de plantilla aprobada (conversación y "Nuevo mensaje") --- */
  function iniciarSelector(select) {
    if (select.dataset.listo) return;
    select.dataset.listo = '1';
    var grupo = select.closest('[data-grupo-plantilla]') || select.form;
    var campos = grupo.querySelector('[data-variables]');
    var previa = grupo.querySelector('[data-vista-previa]');
    var previos = leerValores(campos);

    function opcion() { return select.options[select.selectedIndex]; }
    function actualizarPrevia() {
      var op = opcion();
      if (previa) pintarVistaPrevia(previa, op && op.value ? op.getAttribute('data-cuerpo') : '', valoresDe(campos));
    }
    function alCambiar() {
      var op = opcion();
      campos.textContent = '';
      construirCampos(campos, op && op.value ? +op.getAttribute('data-vars') : 0, 'var', 'Valor para', previos);
      previos = [];
      actualizarPrevia();
    }
    select.addEventListener('change', alCambiar);
    campos.addEventListener('input', actualizarPrevia);
    alCambiar();
  }

  /* --- Formulario de creación de plantilla --- */
  function iniciarCreacion(form) {
    if (form.dataset.listo) return;
    form.dataset.listo = '1';
    var nombre = form.querySelector('[data-nombre-plantilla]');
    var cuerpo = form.querySelector('[data-cuerpo-plantilla]');
    var ejemplos = form.querySelector('[data-ejemplos]');
    var previa = form.querySelector('[data-vista-previa-plantilla]');
    var advertencia = form.querySelector('[data-advertencia]');
    var contador = form.querySelector('[data-contador]');
    var previos = leerValores(ejemplos);

    nombre.addEventListener('input', function () {
      var limpio = nombre.value.normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase()
        .replace(/[\s-]+/g, '_').replace(/[^a-z0-9_]/g, '');
      if (limpio !== nombre.value) nombre.value = limpio;
    });

    function analizar() {
      var texto = cuerpo.value;
      var nums = numerosVariables(texto);
      var maximo = nums.length ? nums[nums.length - 1] : 0;
      var problemas = [];
      if (nums.some(function (n, i) { return n !== i + 1; })) problemas.push('Las variables deben ser consecutivas y empezar en {{1}}.');
      if (/^\s*\{\{/.test(texto)) problemas.push('No puede empezar con una variable.');
      if (/\}\}\s*$/.test(texto)) problemas.push('No puede terminar con una variable.');
      advertencia.textContent = problemas.join(' ');
      advertencia.hidden = !problemas.length;
      contador.textContent = texto.length + ' / 1024';

      construirCampos(ejemplos, maximo, 'ejemplo', 'Ejemplo para', previos);
      previos = [];  // los valores devueltos por el servidor solo se usan en la primera carga
      pintarVistaPrevia(previa, texto.trim(), valoresDe(ejemplos));
    }

    form.querySelector('[data-agregar-variable]').addEventListener('click', function () {
      var nums = numerosVariables(cuerpo.value);
      var token = '{{' + ((nums.length ? nums[nums.length - 1] : 0) + 1) + '}}';
      var ini = cuerpo.selectionStart, fin = cuerpo.selectionEnd;
      cuerpo.setRangeText(token, ini, fin, 'end');
      cuerpo.focus();
      analizar();
    });
    cuerpo.addEventListener('input', analizar);
    ejemplos.addEventListener('input', function () { pintarVistaPrevia(previa, cuerpo.value.trim(), valoresDe(ejemplos)); });
    analizar();
  }

  /* --- Caja de respuesta: Enter envía, Shift+Enter hace salto de línea --- */
  function iniciarTextarea(area) {
    if (area.dataset.listo) return;
    area.dataset.listo = '1';
    function ajustar() { area.style.height = 'auto'; area.style.height = Math.min(area.scrollHeight + 2, 160) + 'px'; }
    area.addEventListener('input', ajustar);
    area.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        if (area.value.trim()) area.form.requestSubmit();
      }
    });
  }

  /* --- Base de conocimiento: contadores y "Cargar ejemplo: clínica" (rellena sin guardar) --- */
  function iniciarConocimiento(form) {
    if (form.dataset.listo) return;
    form.dataset.listo = '1';
    var areas = buscar(form, 'textarea[data-seccion]');
    function contar(area) {
      var etiqueta = area.closest('label').nextElementSibling;
      var contador = etiqueta && etiqueta.querySelector('[data-contador-seccion]');
      if (contador) contador.textContent = area.value.length + ' / ' + area.maxLength;
    }
    areas.forEach(function (area) { contar(area); area.addEventListener('input', function () { contar(area); }); });

    var boton = form.querySelector('[data-cargar-ejemplo]');
    if (!boton) return;
    boton.addEventListener('click', function () {
      var ejemplo;
      try { ejemplo = JSON.parse(boton.getAttribute('data-ejemplo')); } catch (e) { return; }
      var hayTexto = areas.some(function (a) { return a.value.trim(); });
      if (hayTexto && !window.confirm('Esto reemplazará el texto de todas las secciones en el formulario (todavía no se guarda). ¿Continuar?')) return;
      areas.forEach(function (area) {
        if (ejemplo[area.name] != null) { area.value = ejemplo[area.name]; contar(area); }
      });
      areas[0].focus();
    });
  }

  function iniciar(raiz) {
    buscar(raiz, '[data-selector-plantilla]').forEach(iniciarSelector);
    buscar(raiz, '[data-form-plantilla]').forEach(iniciarCreacion);
    buscar(raiz, '[data-enviar-con-enter]').forEach(iniciarTextarea);
    buscar(raiz, '[data-form-conocimiento]').forEach(iniciarConocimiento);
    var sim = document.getElementById('sim-mensajes');
    if (sim && raiz.contains && raiz.contains(sim)) sim.scrollTop = sim.scrollHeight;
  }

  /* --- Scroll de la conversación y refresco del compositor --- */
  var pegadoAbajo = true;
  var scrollPrevio = 0;

  function bajarMensajes() {
    var m = document.getElementById('mensajes');
    if (m) m.scrollTop = m.scrollHeight;
  }

  document.addEventListener('htmx:beforeSwap', function (e) {
    var m = document.getElementById('mensajes');
    if (!m) return;
    var esEnvio = e.detail.requestConfig && e.detail.requestConfig.verb === 'post';
    pegadoAbajo = esEnvio || m.scrollHeight - m.scrollTop - m.clientHeight < 80;
    scrollPrevio = m.scrollTop;
  });

  document.addEventListener('htmx:afterSettle', function () {
    var m = document.getElementById('mensajes');
    if (!m) return;
    m.scrollTop = pegadoAbajo ? m.scrollHeight : scrollPrevio;

    // Si la ventana de 24 h cambió (el cliente escribió o expiró), se recarga solo el compositor.
    var compositor = document.getElementById('composer');
    if (compositor && window.htmx && m.getAttribute('data-ventana') !== compositor.getAttribute('data-modo')) {
      window.htmx.ajax('GET', m.getAttribute('hx-get'), { target: '#composer', select: '#composer', swap: 'outerHTML' });
    }
  });

  /* --- CRM: panel de detalles desplegable (pantallas angostas) --- */
  document.addEventListener('click', function (e) {
    var bandeja = document.querySelector('[data-bandeja]');
    if (!bandeja) return;
    if (e.target.closest('[data-abrir-detalles]')) bandeja.classList.add('mostrar-detalles');
    if (e.target.closest('[data-cerrar-detalles]')) bandeja.classList.remove('mostrar-detalles');
  });

  /* --- CRM: al elegir "Perdido" se pide el motivo --- */
  function actualizarMotivo(select) {
    var form = select.closest('[data-form-etapa]');
    var bloque = form && form.querySelector('[data-motivo]');
    if (!bloque) return;
    var opcion = select.options[select.selectedIndex];
    var perdido = opcion && opcion.getAttribute('data-perdido') === '1';
    bloque.hidden = !perdido;
    var motivo = bloque.querySelector('select[name=motivo]');
    if (motivo) motivo.required = perdido;
  }

  /* --- CRM: pipeline (arrastrar y soltar, menú "Mover a…" y diálogo de pérdida) --- */
  function enviarMovimiento(contacto, etapa, extra) {
    var valores = { etapa_id: etapa, vista: 'pipeline' };
    Object.keys(extra || {}).forEach(function (k) { valores[k] = extra[k]; });
    window.htmx.ajax('POST', '/contactos/' + contacto + '/etapa',
      { target: '#tablero', select: '#tablero', swap: 'outerHTML', values: valores });
  }

  function moverEtapa(contacto, etapa, esPerdido, alCancelar) {
    if (!esPerdido) { enviarMovimiento(contacto, etapa); return; }
    var dialogo = document.getElementById('dialogo-perdido');
    var form = dialogo && dialogo.querySelector('form');
    if (!form || !dialogo.showModal) { if (alCancelar) alCancelar(); return; }
    form.reset();
    dialogo.onclose = function () {
      if (dialogo.returnValue === 'confirmar' && form.motivo.value) {
        enviarMovimiento(contacto, etapa, { motivo: form.motivo.value, motivo_detalle: form.motivo_detalle.value });
      } else if (alCancelar) {
        alCancelar();
      }
    };
    dialogo.showModal();
  }

  /* --- Panel de modelos: el comparador admite como máximo 3 modelos --- */
  document.addEventListener('change', function (e) {
    var casilla = e.target;
    var form = casilla.closest && casilla.closest('[data-comparador]');
    if (!form || casilla.name !== 'modelo_ids' || !casilla.checked) return;
    var maximo = parseInt(form.getAttribute('data-max'), 10) || 3;
    if (buscar(form, 'input[name=modelo_ids]:checked').length > maximo) {
      casilla.checked = false;
      window.alert('Puedes comparar como máximo ' + maximo + ' modelos a la vez.');
    }
  });

  document.addEventListener('change', function (e) {
    var select = e.target;
    if (select.matches('[data-select-etapa]')) actualizarMotivo(select);
    if (select.matches('[data-mover-contacto]') && select.value) {
      var opcion = select.options[select.selectedIndex];
      moverEtapa(select.getAttribute('data-mover-contacto'), select.value,
        opcion.getAttribute('data-perdido') === '1', function () { select.value = ''; });
    }
  });

  var arrastrado = null;
  document.addEventListener('dragstart', function (e) {
    var tarjeta = e.target.closest && e.target.closest('.tarjeta-contacto');
    if (!tarjeta) return;
    arrastrado = tarjeta;
    tarjeta.classList.add('arrastrando');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', tarjeta.getAttribute('data-contacto'));
  });
  document.addEventListener('dragend', function () {
    if (arrastrado) arrastrado.classList.remove('arrastrando');
    arrastrado = null;
    buscar(document, '.columna.sobre').forEach(function (c) { c.classList.remove('sobre'); });
  });
  document.addEventListener('dragover', function (e) {
    var columna = arrastrado && e.target.closest && e.target.closest('.columna');
    if (!columna) return;
    e.preventDefault();
    columna.classList.add('sobre');
  });
  document.addEventListener('dragleave', function (e) {
    var columna = e.target.closest && e.target.closest('.columna');
    if (columna && !columna.contains(e.relatedTarget)) columna.classList.remove('sobre');
  });
  document.addEventListener('drop', function (e) {
    var columna = arrastrado && e.target.closest && e.target.closest('.columna');
    if (!columna) return;
    e.preventDefault();
    columna.classList.remove('sobre');
    if (columna.contains(arrastrado)) return;  // misma columna
    moverEtapa(arrastrado.getAttribute('data-contacto'), columna.getAttribute('data-etapa'),
      columna.getAttribute('data-perdido') === '1');
  });

  document.addEventListener('htmx:load', function (e) { iniciar(e.detail.elt); });
  document.addEventListener('DOMContentLoaded', function () {
    iniciar(document);
    bajarMensajes();
    var sim = document.getElementById('sim-mensajes');
    if (sim) sim.scrollTop = sim.scrollHeight;
    var texto = document.querySelector('#simulador textarea');
    if (texto) texto.focus({ preventScroll: true });
  });
  document.addEventListener('htmx:afterSettle', function () {
    var sim = document.getElementById('sim-mensajes');
    if (sim) {
      sim.scrollTop = sim.scrollHeight;
      var texto = sim.parentNode.querySelector('textarea');
      if (texto) texto.focus({ preventScroll: true });
    }
  });
})();
