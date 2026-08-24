# BalanzaPrivada

Utilidad de línea de comandos, completamente local, para anonimizar PDF **digitales** de balanzas de comprobación. Conserva importes, saldos, códigos de cuenta, tablas, páginas y orientación; genera otro PDF y un reporte técnico sin datos sensibles.

No usa OCR, IA, APIs, red ni servicios externos. Si PyMuPDF no puede extraer texto con coordenadas fiables, no reconoce un perfil con confianza suficiente, no puede redactar un dato obligatorio o falla la verificación posterior, el archivo falla de forma segura y no se entrega como exitoso.

## Requisitos e instalación

- Python 3.11 o superior.
- macOS, Linux o Windows con una compilación compatible de PyMuPDF.

En PyCharm, abra esta carpeta como el proyecto **BalanzaPrivada** y seleccione un intérprete Python 3.11. Desde una terminal:

```bash
python3.11 -m venv .venv
source .venv/bin/activate  # En Windows: .venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Uso

La entrada puede ser un PDF o un directorio no recursivo de PDFs. La semilla nunca se escribe al PDF ni al reporte; guárdela en un gestor de secretos.

```bash
python anonymize_balances.py \
  --input ./entrada \
  --output ./salida \
  --seed "una-semilla-secreta-larga" \
  --strict \
  --report ./salida/reporte.json
```

Los PDF resultantes se llaman `anonimizado_<id>.pdf`: no reutilizan el nombre de la fuente y nunca sobrescriben ni el original ni una salida previa. El proceso devuelve código `0` únicamente cuando todos los archivos terminan correctamente; devuelve `2` si alguno falla. La consola no muestra texto extraído ni nombres de archivo fuente.

Para inspeccionar detecciones sin crear PDFs:

```bash
python anonymize_balances.py \
  --input ./entrada/balanza.pdf \
  --output ./salida \
  --seed "una-semilla-secreta-larga" \
  --dry-run
```

`--strict` es el modo predeterminado y rechaza perfiles con advertencias; las comprobaciones que podrían producir una anonimización incompleta nunca se relajan.

### Regiones de logotipo vectorial configuradas

No se borran líneas ni gráficos por heurística. Si un formato contiene un logotipo vectorial conocido, puede proporcionar de forma explícita las regiones a redactar, en puntos PDF y con páginas basadas en cero:

```json
{
  "tipo_2": [
    {"page": 0, "rect": [420, 20, 550, 80]}
  ]
}
```

Úselo con `--vector-logo-regions regiones.json`. Todas las imágenes rasterizadas incrustadas se eliminan automáticamente; las regiones configuradas solo complementan ese tratamiento.

## Perfiles y reglas aplicadas

- **Tipo 1:** anclas `Nombre/Razón Social`, `RFC`, `Fecha creación`, `Ejercicio` y `Período`. Cambia fecha/hora de creación, ejercicio/periodo de forma coherente, razón social y RFC.
- **Tipo 2:** anclas `Reg. fed.`, `Dirección`, `Población`, `Cédula` y `Balanza de comprobación al`. Localiza cabeceras no etiquetadas solo cuando se pueden vincular contextualmente con un RFC fiscal.
- **Tipo 3:** ancla `CONTPAQ i`. El logo textual se elimina; se sustituyen la razón social de cabecera y las fechas de cabecera, creación, carga o generación.
- **Tipo 4:** solo cambia números con separadores y longitud suficiente cuando están en una línea con contexto bancario. No trata fechas, importes ni códigos contables como cuentas.

La vinculación entidad–RFC se hace con el RFC normalizado; cuando no hay RFC, se usa la razón social normalizada. Los seudónimos se derivan con HMAC-SHA256 y la semilla indicada. Por ello, la misma entidad recibe el mismo nombre/RFC/domicilio sintéticos en todo el lote y entre ejecuciones que usen la misma semilla. No se persiste ningún diccionario de equivalencias.

Una razón social aislada dentro de una descripción contable no se modifica. Por ejemplo, un texto de proveedor sin RFC asociado permanece intacto.

## Seguridad y verificación

Para cada dato detectado se añade una anotación de redacción y se aplica `apply_redactions`; no se superponen rectángulos blancos como sustituto. Después se inserta el seudónimo ajustando su tamaño dentro de la caja original. El archivo se guarda con limpieza de objetos, compresión y saneamiento (`garbage=4`, `deflate=True`, `clean=True`).

También se eliminan metadatos estándar, XMP y archivos adjuntos. Antes de aceptar una salida, la herramienta la reabre y comprueba:

1. La misma cantidad, dimensiones y posibilidad de renderizado de las páginas.
2. La ausencia de cada valor detectado en el texto extraíble y en streams de contenido accesibles.
3. La ausencia de PII detectada en los metadatos, XMP y adjuntos.

El reporte JSON contiene perfil, páginas, categorías, coordenadas y hashes HMAC de detecciones. No contiene valores detectados, sustituciones, semilla, rutas fuente ni diccionarios de correspondencia.

## Limitaciones deliberadas

- Solo se admiten PDFs digitales con texto posicionable. Escaneos, fotografías, páginas convertidas a imagen y PDFs vacíos se rechazan; no hay OCR.
- No se valida ni consulta el SAT. Los RFC de salida tienen 12 o 13 caracteres y estructura visual equivalente, pero son sintéticos.
- Fechas de movimientos internos se conservan. Solo se cambian fechas identificadas en cabecera como creación, carga, emisión, generación o corte.
- Si los campos obligatorios quedan ambiguos o el texto no cabe sin invadir contenido, el archivo falla. Revise el reporte seguro y, si procede, configure únicamente regiones vectoriales explícitas.
- Las reglas se orientan a las familias de formatos indicadas; documentos con maquetaciones nuevas deben revisarse primero con `--dry-run`.

## Pruebas

Las pruebas no usan documentos reales ni archivos externos; generan PDFs digitales sintéticos para los perfiles 1 a 4.

```bash
python -m pytest
```

Cubren redacción de los campos requeridos, consistencia del par razón social/RFC, preservación de importes y códigos, eliminación de logo raster/textual, metadatos, dimensiones, páginas, reporte seguro y rechazo de un PDF sin texto extraíble.
