# BalanzaPrivada

Utilidad local de línea de comandos para anonimizar lotes de balanzas de
comprobación en PDF digital, XLSX y XML de contabilidad electrónica. Conserva
códigos, importes, saldos, movimientos, fórmulas y estructura; sustituye la
identidad del propietario, metadatos temporales de cabecera y entidades que sí
estén asociadas con un identificador dentro de una descripción contable.

La ejecución no usa APIs, servicios externos, internet, OCR ni modelos de IA.
Los archivos fuente nunca se sobrescriben.

## Requisitos e instalación

- Python 3.11 o superior.
- PyMuPDF para PDF digital.
- openpyxl y Pillow para OOXML/XLSX.
- lxml para XML seguro.
- PyYAML para manifiestos YAML; también se admite JSON.

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Ejecución recomendada

```bash
export BALANCE_ANON_SEED='una-semilla-secreta-de-al-menos-16-caracteres'

python anonymize_balances.py \
  --input ./entrada \
  --output ./salida \
  --manifest ./equivalence.yml \
  --registry ./estado/anon_registry.sqlite \
  --seed-env BALANCE_ANON_SEED \
  --strict \
  --report ./salida/reporte.json
```

`--input` acepta un archivo o un directorio no recursivo. PDF, XLSX y XML se
procesan; los `.xls` binarios se incluyen en el reporte como `UNSUPPORTED_XLS`
para que nunca desaparezcan silenciosamente del conteo. Deben exportarse a
XLSX desde una aplicación de confianza antes de anonimizarse. El modo estricto
es el predeterminado. La utilidad devuelve `0` si todos los archivos terminan
correctamente y `2` si existe un rechazo o un grupo fallido.

La semilla puede recibirse mediante:

- `--seed-env NOMBRE_VARIABLE`, recomendado para automatización;
- `--seed-file /ruta/protegida/semilla.txt`, una sola línea;
- `--seed`, conservado por compatibilidad y acompañado de una advertencia
  porque puede quedar en el historial del terminal;
- entrada oculta interactiva, cuando no se proporcionó otra fuente.

La semilla debe tener al menos 16 caracteres. No se almacena en el registro,
reporte ni documentos generados.

## Descubrimiento y modo de prueba

```bash
# Propone relaciones, sin crear documentos ni escribir el registro.
python anonymize_balances.py \
  --input ./entrada \
  --output ./salida \
  --discover \
  --manifest-proposal ./equivalence.propuesto.yml \
  --seed-env BALANCE_ANON_SEED \
  --report ./salida/descubrimiento.json

# Evalúa detecciones y bloqueos estrictos, sin crear documentos.
python anonymize_balances.py \
  --input ./entrada \
  --output ./salida \
  --dry-run \
  --seed-env BALANCE_ANON_SEED \
  --report ./salida/dry-run.json
```

El nombre del archivo nunca determina una relación. Se usan códigos de cuenta
normalizados, `Decimal` para los cuatro importes canónicos, naturaleza cuando
existe y período/moneda solo como señales adicionales. Nombre, RFC, dirección,
logos, fecha de impresión y descripciones sensibles quedan fuera de la huella
de contenido.

Las relaciones posibles son:

- `exact_equivalent`: mismas cuentas e importes;
- `projection`: selección o distinto nivel de detalle con renglones compartidos
  iguales;
- `series`: períodos consecutivos con continuidad contable; nunca se trata como
  equivalencia;
- `ambiguous`: evidencia insuficiente, colisión de normalización o conflicto;
- `standalone`: documento procesable sin pareja.

`--manifest-proposal` genera un borrador YAML o JSON con rutas y IDs opacos
para todas las relaciones inferidas. No confirma conflictos ni elige
`metadata_source`: esos elementos quedan comentados para revisión humana.
La propuesta nunca sobrescribe un archivo existente.

## Arquitectura de dos pasadas

En la primera pasada cada adaptador produce un `DocumentSnapshot` canónico sin
modificar el archivo. El modelo incluye `OwnerIdentity`, `TemporalMetadata`,
`LedgerLine`, `SensitiveSpan` y ubicaciones concretas del formato. Cada importe
se representa como `Decimal` y conserva también su texto original.

Después se infieren o confirman `DocumentRelation`, se resuelven conflictos y se
construye un único `AnonymizationPlan` por entidad y grupo. En la segunda pasada
el mismo plan se entrega a PDF, XLSX y XML. Todos los miembros se generan en un
directorio temporal, se reabren, se validan individualmente y se validan de
forma cruzada. Solo entonces se promueve el grupo completo. Si un miembro falla,
ninguno queda marcado ni publicado como exitoso.

## Manifiesto de relaciones

Use un manifiesto para confirmar grupos, series, entidad y fuente autorizada de
metadatos sin guardar PII. Las rutas relativas se resuelven respecto del propio
manifiesto.

```yaml
groups:
  - id: balance_projection_001
    relation: projection
    entity_id: entity_001
    files:
      - entrada/proyeccion.xlsx
      - entrada/mayor.xml
    metadata_source: entrada/proyeccion.xlsx

series:
  - id: balance_series_002
    entity_id: entity_002
    files:
      - entrada/periodo_anterior.pdf
      - entrada/periodo_siguiente.xml
```

Los identificadores deben ser opacos; nombres, RFC, direcciones y otros datos
identificables están prohibidos. Consulte `equivalence.example.yml` y
`equivalence.schema.json`.

En modo estricto, un conflicto de propietario exige una confirmación de entidad
o fuente autorizada. Un conflicto de período o fecha entre documentos
equivalentes exige `metadata_source`. El programa no toma el primer archivo como
fuente de verdad.

## Seudonimización y registro SQLite

Todos los valores sintéticos se derivan con HMAC-SHA256. La clave canónica usa,
en orden, `entity_id`, RFC normalizado, nombre normalizado o un identificador
interno de grupo. El desplazamiento temporal se calcula en meses completos para
preservar secuencia, fin de mes y continuidad entre períodos.

El registro SQLite versionado mantiene consistencia entre lotes. Solo guarda:

- HMAC del identificador normalizado;
- namespace (`owner`, `rfc`, `bank`, `account`, `address` o `date`);
- tipo de token y valor sintético;
- versión del algoritmo;
- conteos de colisión sin PII.

Nunca guarda el original, la semilla ni una tabla reversible. Use la misma
semilla y el mismo registro para conservar asignaciones entre ejecuciones. No
comparta el registro como si fuera un documento público: aunque no contiene
originales, forma parte del estado de seguridad de la instalación.

Los RFC generados tienen 12 caracteres para persona moral o 13 para persona
física, incorporan una fecha sintácticamente válida y no se presentan como RFC
reales o validados por el SAT.

## Comportamiento por formato

### PDF

Se conserva el motor previo de redacción física con PyMuPDF. Los originales se
eliminan mediante redacciones reales y los reemplazos se insertan dentro del
campo autorizado. Se preservan geometría, tablas, bordes, dibujos e imágenes no
clasificadas como logo. La verificación reabre el PDF, revisa texto y streams,
metadatos, imágenes, dibujos, números fuera de regiones autorizadas y diferencia
visual. Solo se admiten PDFs digitales de las familias conocidas; no existe OCR.

`--vector-logo-regions regiones.json` conserva la opción previa para regiones
vectoriales de logo explícitamente autorizadas.

### XLSX

Se abre en modo normal con `data_only=False` y `keep_links=True`. La detección se
basa en etiquetas, estructura, tipos y contexto; no en coordenadas fijas. Se
modifican únicamente celdas o componentes sensibles y se conservan fórmulas,
tipos, estilos, formatos numéricos, dimensiones, combinaciones, filtros, tablas,
validaciones, configuración de impresión, gráficas e imágenes no clasificadas
como logo.

Se inspeccionan hojas ocultas, comentarios, encabezados/pies, propiedades,
fechas internas, hipervínculos, nombres definidos, cadenas y partes OOXML. Tras
guardar, el libro se reabre, se comparan celdas y estructura no objetivo y se
inspecciona el ZIP para detectar originales residuales, medios de logo o
relaciones huérfanas. Los `.xlsm` se rechazan explícitamente; no se convierten a
`.xlsx`. En modo estricto también se rechazan ActiveX, OLE, VBA y objetos
incrustados no soportados.

### XML

lxml se configura sin resolución de entidades, sin DTD, sin red y con límites
de tamaño/profundidad. Se detecta por nombres expandidos de namespace, no por el
prefijo. Se anonimizan `RFC`, `Anio`, `Mes` y `FechaModBal` cuando existe. Se
preservan versión, tipo de envío, namespace/prefijo, `schemaLocation`, orden de
`Ctas`, `NumCta` y las cadenas decimales exactas, incluidos signos, ceros y
escala.

Un XML con DTD o entidades se rechaza. Si aparecen `Sello`, `Certificado` o
`noCertificado`, el modo estricto devuelve `SIGNATURE_PRESENT`. Solo la opción
explícita `--strip-signature` elimina esos atributos y marca el resultado como
anonimizado no apto para envío fiscal. Nunca se firma ni se solicitan llaves.

Puede validarse la salida con un esquema local mediante `--xsd esquema.xsd`; el
programa nunca descarga esquemas.

## Regla para descripciones contables

Una persona, empresa o banco dentro de una descripción se sustituye solo cuando
la misma descripción lógica contiene RFC, cuenta, CLABE, tarjeta, cliente,
`CTA`/`CUENTA` u otro identificador de más de dos dígitos. Se sustituyen entidad
e identificador con una clave común y se conservan prefijos, puntuación y patrón
de dígitos cuando es posible.

Entidades sin identificador permanecen intactas. Códigos de la columna Cuenta,
importes, saldos, fechas internas, pólizas y referencias ordinarias no activan
esta regla por sí solos.

## Reporte técnico

El JSON incluye adaptador/perfil, grupo, relación, confianza, conflictos,
conteos de sustituciones, validaciones, advertencias, estado atómico y HMAC de
detecciones. No incluye rutas fuente, PII original, texto completo sensible,
RFC original, semilla, seudónimos asignados ni equivalencias reversibles.

## Recuperación ante errores

- Un fallo de descubrimiento no crea salida.
- Un fallo durante la segunda pasada descarta todos los temporales del grupo.
- Una salida ya existente nunca se sobrescribe; use otro directorio o archive
  la ejecución anterior.
- Corrija el manifiesto o la estructura no soportada y repita con la misma
  semilla y registro.
- Haga copias del registro únicamente con el proceso cerrado. Si el registro se
  pierde, restaure una copia; crear uno nuevo puede producir otra identidad
  sintética para lotes posteriores.
- Un XML procesado con `--strip-signature` no debe enviarse al SAT.

## Pruebas

```bash
# Suite versionable con PDF, XLSX y XML sintéticos.
python -m pytest

# Integración opcional con muestras saneadas fuera del repositorio.
BALANCE_SAMPLE_DIR=/ruta/a/muestras \
  python -m pytest -m private_integration
```

La suite cubre el motor PDF existente, seudonimización compartida, registro
SQLite, equivalencia/proyección/serie, conflicto de metadatos, independientes,
logos, OOXML, fórmulas/estilos/combinaciones, reglas de descripciones, XML
seguro, firmas y promoción atómica.

Las muestras privadas o saneadas no deben incorporarse al repositorio. Use
`.private-fixtures/` y `.private-validation/`, rutas ignoradas por Git.

## Limitaciones deliberadas

- No hay OCR, procesamiento de escaneos ni rasterización como fallback.
- Un perfil PDF nuevo falla hasta incorporar anclas y pruebas.
- Solo se eliminan logos XLSX/PDF clasificados con alta confianza; elementos
  funcionales se preservan.
- No se consultan RFC ni información fiscal contra el SAT.
- No se reparan automáticamente namespaces, `schemaLocation`, contabilidad ni
  documentos con estructuras criptográficas/embebidas no soportadas.
