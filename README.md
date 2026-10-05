# naau-worker

Lee una planilla de corte de fierros —en PDF, o fotografiada— y devuelve **lo
que el papel dice**.

Es un proceso aparte, chico y sin estado. No habla con la base de datos, no
escribe a disco, no guarda el archivo y no conoce las empresas ni los usuarios
del sistema. Todo eso vive en `naau-server`, que es quien autentica a la
persona, quien sabe de qué empresa es y quien decide qué se guarda.

## Por qué existe

Una planilla no es texto: es una tabla delimitada por trazos vectoriales, y
reconstruirla es agrupar caracteres por sus coordenadas contra las líneas de
regla. `pdfplumber` ya hace exactamente eso, y rehacerlo en Node sería
reimplementar una biblioteca madura.

Y cuando la planilla llega **fotografiada**, no hay ni trazos ni texto: hay
píxeles. Reconstruir la tabla de ahí es encontrar la regla impresa, armar la
grilla y reconocer cada celda — visión por computadora y OCR, que es el otro
terreno donde Python tiene las bibliotecas maduras.

Esa es la **única** razón por la que hay Python en este sistema. No hay cola, no
hay Redis, no hay bucket: una planilla de 27 posiciones se lee en menos de dos
segundos, así que la petición es sincrónica y el archivo viaja en memoria y se
descarta.

## La API

```
GET  /health                  → {"status": "ok"}
GET  /template/schedule-xlsx  ?unit=cm|m  → el .xlsx vacío para llenar a mano
                              header X-Worker-Token: <token>
POST /export/schedule-xlsx    cuerpo JSON con la planilla cargada
                              → el .xlsx con sus filas puestas
                              header X-Worker-Token: <token>
POST /parse/schedule-pdf      multipart, campo `file`
                              header X-Worker-Token: <token>
```

### Por qué este proceso también ESCRIBE

Porque el `.xlsx` que baja NAAU se puede volver a subir, y eso sólo se sostiene
si los encabezados que se escriben son exactamente los que este mismo módulo
reconoce al leer. Quien sabe cómo se rotula una planilla de fierros en Excel es
uno solo —`xlsx_table`— y por eso las tres rutas de Excel viven acá.

`naau-server` manda los datos con su papel —`pos`, `type`, `measures`,
`unit_length`— y acá se decide cómo se rotula cada columna. Escrito del otro
lado, un rótulo cambiado de un solo lado rompería el viaje de vuelta sin que
nada lo avise: el archivo se bajaría igual de bien y volvería con la mitad de
las columnas sin reconocer.

### La lectura

Una sola ruta para las tres cosas. Qué llegó lo deciden los **primeros bytes**
del archivo —`%PDF-`, la firma de un PNG, JPEG o WEBP, o `PK` de un ZIP— y no el
nombre ni el `content-type`, que los pone quien sube. El resultado dice por cuál
de los tres caminos se leyó, en `source`.

La firma del XLSX merece una aclaración: un `.xlsx` **es** un ZIP, así que esos
cuatro bytes los comparte con un `.docx`, un `.jar` y cualquier carpeta
comprimida. La firma no pretende afirmar que sea un libro de cálculo — pretende
que al decodificador no le llegue un video. Distinguir un libro de una carta es
trabajo de abrirlo, y ahí sale `CORRUPT_EXCEL` con un mensaje que dice qué
hacer.

| Respuesta | Cuándo | Qué hacer |
|---|---|---|
| `200` | Se leyó | Revisar y guardar |
| `401 UNAUTHORIZED` | Falta el token o no coincide | Revisar `NAAU_WORKER_TOKEN` en los dos lados |
| `400 EMPTY_FILE` | No llegó contenido | — |
| `413 FILE_TOO_LARGE` | Más de 8 MB | — |
| `415 NOT_A_PDF` | El contenido no es un PDF, ni una imagen aceptada, ni un ZIP | Subir un PDF, PNG, JPEG, WEBP o XLSX |
| `422 CORRUPT_PDF` | Es un PDF y no se pudo abrir | Volver a exportarlo |
| `422 CORRUPT_EXCEL` | Es un ZIP y no un libro que se pueda abrir | Volver a guardarlo como .xlsx |
| `422 EXCEL_TOO_LARGE` | Más de 20 000 celdas con contenido | Dejar en el libro sólo la hoja de la planilla |
| `422 EXCEL_UNAVAILABLE` | Falta `openpyxl` en el worker | Es del servidor: instalar dependencias |
| `422 CORRUPT_IMAGE` | Es una imagen y no se pudo decodificar | Se cortó al subirla; volver a sacar la foto |
| `422 IMAGE_TOO_LARGE` | Más de 40 megapíxeles | Sacar la foto con menos resolución |
| `422 IMAGE_TOO_SMALL` | Menos de 60 px por columna **y** la planilla no declara longitud ni total | Subir el PDF, o escanear/fotografiar más grande |
| `422 NO_TABLE` | No hay líneas de regla | La tabla tiene que estar recuadrada, y la foto mostrarla completa y de frente |
| `422 NO_TEXT` | Hay tabla y no se reconoció un solo texto | Más cerca, con luz y sin sombras |
| `422 NO_HEADER` | No se reconoció el encabezado | Hace falta una columna de posición y medidas rotuladas con letras |
| `422 NO_ROWS` | Encabezado sí, filas no | — |

Todos los errores salen con la misma forma, `{"error": {"code", "message"}}`,
para que el servidor tenga un solo caso que traducir.

## Lo que devuelve, y lo que no

Devuelve lo que la planilla **afirma**, no lo que es correcto. Las columnas de
resultado —longitud, barras, peso— viajan bajo la clave `claimed`, con ese
nombre a propósito: `naau-server` las usa para una sola cosa, compararlas contra
lo que calcula el motor propio y mostrar las dos cifras cuando no coinciden.

No devuelve un puntaje de confianza. No tendría con qué calcularlo: el worker no
sabe de fierros. El motor de NAAU sí, y ahí es donde se verifica.

Lo que sí devuelve es `source`: `pdf` cuando el texto se **leyó**, `image`
cuando se **reconoció** y `xlsx` cuando la celda trae el número que alguien
escribió. No es telemetría — es cuánta confianza merece todo lo demás, y es lo
que hace que la pantalla pida revisar medida por medida en un caso y no en los
otros dos.

### El XLSX entra por el mismo lector, y por eso es tan poco código

Un `.xlsx` no es un documento distinto: es la **misma matriz de celdas** que
`pdfplumber` entrega de un PDF y que el reconocimiento entrega de una foto, sólo
que llega sin pérdida. Así que `parse_xlsx` tiene doce líneas y de ahí en
adelante corre el mismo camino: el mapeo de columnas por sinónimos, las dos
unidades, la deducción de la celda que el documento determina y la verificación
fila por fila.

Un lector de Excel propio habría sido un SEGUNDO camino de entrada con su propia
idea de qué es una fila válida — y la clase de desfase que termina en una
planilla que se lee distinto según en qué formato llegó el mismo dato.

**Cada hoja es una «página».** El lector elige cuál es la tabla de posiciones
igual que elige entre las páginas de un PDF: mirando los rótulos. Por eso la
hoja de instrucciones de la plantilla no molesta —tiene otro ancho y otros
rótulos— y no hace falta pedirle a nadie que la borre ni exigir que la hoja de
datos se llame de una manera.

**La plantilla la genera este mismo módulo**, y no es comodidad: los encabezados
que escribe son exactamente los que `mapear_columnas` reconoce, y el rótulo de
unidad de las columnas de medida —«a (cm)»— es el que `_unidad_declarada` lee.
Generada en cualquier otro lado, un rótulo cambiado dejaría de mapearse sin que
nada lo avise: el archivo se seguiría descargando igual de bien y volvería con
las columnas sin reconocer, con toda la apariencia de ser culpa de quien lo
llenó. Hay una prueba que lo fija: `test_todos_los_encabezados_de_la_plantilla_se_reconocen`.

**Lo único ambiguo que puede traer un libro** es un número escrito como TEXTO
con coma decimal. Excel guarda el *valor* de una celda numérica y no cómo se ve
—la misma celda se muestra «120,5» en una computadora en español y «120.5» en
una en inglés, y el archivo dice 120.5 en las dos— así que el separador decimal
no se adivina: se sabe. Pero una celda de texto no trae valor, trae esas cuatro
letras, y se lee 615.

No se corrige. Corregirla sería decidir por cuenta propia que la coma es un
decimal y no un separador de miles, sobre la única celda del libro que no trae
su valor. Se nombra en los avisos, y quien la escribió la vuelve a escribir.

### El tipo que el documento DECLARA

La columna «TIPO» viaja en `type_code`, crudo. Estuvo sin mapear un tiempo y la
razón era buena pero el remedio estaba errado: figuraba entre los sinónimos del
CROQUIS, así que en la planilla de obra que trae las dos columnas —«TIPO» con la
letra de la figura y «ESQUEMA» con los dibujos— una se quedaba el campo y la
otra caía sin reconocer, en silencio. Y peor: `letras_croquis("J")` devolvía una
letra, o sea «esta figura usa una medida», en una fila que trae cuatro.

Como campo propio no le roba la columna a nadie. Lo que este lector **no** hace
es resolverlo —no conoce el catálogo de la empresa— así que decide Nest, y
decide con el orden correcto: primero cuál de los tipos reproduce la longitud
impresa, que es aritmética, y sólo después lo que la columna declara, que es un
rótulo. Un papel que dice «O» sobre una fila cuya longitud sale de otra fórmula
está declarando algo que sus propios números desmienten.

Ese orden es lo que hace servir a la plantilla de Excel sin abrir un agujero: en
la plantilla no se escribe ninguna longitud —las calcula NAAU, que es para lo
que sirve— así que no hay aritmética que probar y el nombre del tipo es la mejor
evidencia que queda. En un PDF de obra, en cambio, la aritmética está y manda.

### Qué es cada columna: primero el rótulo, después las cuentas

El mapeo de columnas nunca fue por posición — es por diccionario de sinónimos
contra el encabezado impreso, así que **el orden de las columnas da igual** y
siempre dio igual. Lo que fallaba era otra cosa: si el rótulo no está en el
diccionario («PESO +7%») o el reconocimiento lo rompió («LONG. TOT.(m.)» →
«L0MG T0T(rn)»), la columna no se reconoce, y con ella se va la mitad de la
planilla. Ampliar la lista no arregla eso: persigue documentos que todavía no
vimos, y con una foto los rótulos no son ni estables.

Lo que sí lo arregla es que **una planilla de fierros es un sistema de
ecuaciones y se verifica a sí misma**. En cada fila:

    suma de las medidas           = longitud de la pieza   (× el factor de unidad)
    longitud × cantidad × veces   = longitud total

Así que no hace falta preguntarse qué significa una columna: hace falta buscar
el reparto de papeles que hace **cerrar las cuentas del documento**. Eso es
`column_roles.py`, y da tres cosas de un saque — cuáles son las medidas, cuál es
la longitud, y el factor de unidad entre las dos (los 100 que separan los
centímetros de las medidas de los metros de la longitud salen de la cuenta, no
de una suposición).

La cantidad y las veces son el caso bonito: dos columnas de enteros de una
cifra, indistinguibles por contenido. Lo que las ordena es el producto.

Se usa en dos lugares, y en los dos el rótulo manda:

- **Rellena** lo que el encabezado no nombró, y avisa cuál dedujo. Nunca corrige
  un rótulo: si el encabezado dice una cosa y la aritmética otra, el que puede
  estar mal leído es cualquiera de los dos, y cambiar en silencio lo que el
  documento declara es la clase de decisión invisible que este lector no toma.
  Avisa y deja el rótulo.
- **Sostiene el documento entero** cuando no hay ningún encabezado legible, que
  antes era un `NO_HEADER` y ahora se lee. Las medidas se rotulan «a», «b», «c»…
  en orden de columna, que es lo que habría hecho la planilla.

Medido sobre la planilla de obra de 14 columnas, sin un solo rótulo: las doce
posiciones, las cinco medidas en su orden, el diámetro, la cantidad y las veces,
con **24 de 24 ecuaciones cerrando**, en 28 ms. Y con las columnas barajadas al
azar, ocho barajados distintos, lo mismo.

**Y si ninguna asignación cierra, no devuelve nada.** Eso es lo que lo hace
seguro: la salida viene siempre con la cuenta de filas que la respaldan, y un
reparto inventado que nadie confirma no se distingue en la pantalla de uno
correcto. Una tabla de números sin relación entre sí devuelve `None` y las
columnas quedan sin reconocer, a la vista.

Lo que **no** puede deducir son las columnas que no participan de ninguna
ecuación: el tipo de figura, la sección, el croquis. No hay nada contra qué
verificarlas. Siguen saliendo por rótulo, y lo que no se reconoce viaja crudo.

### Una celda que no se leyó, pero que el documento determina

La columna «CANT.» es angosta y sus números tienen una o dos cifras, así que es
la primera que se pierde en una foto. En el documento de obra a 1000 px, **seis
de catorce filas** llegaban a la pantalla con «la fila no trae cantidad de
piezas» y quedaban bloqueadas.

La cantidad no hay que adivinarla. El papel afirma `total = longitud × cantidad
× veces` y de esos cuatro números leyó tres: 46,00 ÷ 5,75 = **8**. Es la
ecuación del documento, no una heurística.

Se despeja bajo dos condiciones, y las dos importan:

1. **La longitud tiene que estar corroborada por la suma de las medidas.** La
   cantidad se despeja DE la longitud, así que si la longitud también pudiera
   estar mal leída el resultado sería una cadena de suposiciones con aspecto de
   dato. Con la suma cerrando, la longitud tiene un respaldo independiente. (Y
   no hace falta saber la unidad: basta que la razón entre la suma y la longitud
   sea una de las que separan mm, cm y m — este documento tiene las dos.)
2. **El resultado tiene que ser un entero y cerrar hacia atrás** con la holgura
   del redondeo del papel. Las piezas se cuentan de una en una: un cociente de
   4,06 no es una cantidad que no se leyó, es una señal de que alguno de los
   otros números está mal, y ahí la celda queda en blanco y la fila bloqueada.

Lo que esto cuesta, y hay que decirlo: la fila **pierde su verificación
cruzada**. Con la cantidad despejada de esa ecuación, la ecuación ya no
comprueba nada — queda el entero y el redondeo, que es menos. Por eso la fila
vuelve con la cuenta escrita («46 ÷ 5.75 = 8, confirmala contra el papel»), que
va a la pantalla de revisión.

### La notación «2x24»: el multiplicador es de la FÓRMULA, no del dato

Las filas de estribos traen las medidas como `2x24`, `2x105`, `2x1`. La cuenta
cierra —2×24 + 2×105 + 2×1 = 260 cm contra 2,60 m declarados— así que la celda
está bien leída. La pregunta es qué se guarda.

**Se guarda M, no N×M.** Y esto se leyó mal dos veces antes de mirar los
números. La primera lectura fue «son tramos repetidos y hay que repartirlos»; la
segunda, «no se reparten porque el orden define la figura y la suma no». Las dos
razonaban sobre el dato. Lo que decide es la fórmula del tipo:

| fila | medidas | `2*(a+b)+c`, el `O` del sistema | `2*(a+b+c)` | el papel declara |
|---|---|---|---|---|
| 3 | 24, 105, 1 | 259 | **260** | 260 |
| 10 | 19, 19, 2 | 78 | **80** | 80 |

El `×2` de cada celda no es parte de la medida: es el `2*(…)` de la fórmula del
estribo, escrito celda por celda porque en el papel no hay dónde poner una
fórmula. Guardar 48 en lugar de 24 sería contar dos veces lo que la fórmula ya
cuenta, y la longitud saldría al doble.

Así que la medida se guarda sola y la fila informa la cuenta hecha, incluida la
pista que hace falta para resolverla: *si ningún tipo del catálogo da esa
longitud, el que falta es un estribo con fórmula `2*(a + b + c)` — el del
sistema suma el gancho una vez y este papel lo cuenta dos*.

Los dos puntos NO se aceptan como multiplicador, y es a propósito: en la foto
del documento de obra `2x24` salió `2:04`, o sea que el reconocedor se equivocó
en la equis **y** en un dígito. Aceptarlos daría «2 tramos de 4» con aire de dato
leído.

### El separador decimal sale de los NÚMEROS, no de la prosa

El estilo decimal del documento —«us» 1,234.56 o «eu» 1.234,56— se decide con la
primera celda que traiga los dos separadores, donde el que está más a la derecha
es necesariamente el decimal. Una celda así resuelve el documento entero.

Miraba **cualquier** celda, y ahí estaba el problema. Una celda de prosa con un
punto y después una coma —«Obra: Torre Sur. Fecha, marzo»— decidía que todo el
documento estaba en estilo europeo, y a partir de ahí «10.5» se leía 105: diez
veces más grande, verosímil, sin una sola señal.

Apareció con la hoja de instrucciones de la plantilla de Excel, cuya primera
oración tiene exactamente esa forma. En un PDF hace falta la misma casualidad y
basta una vez: el título del documento cae en la primera celda de la tabla.
Ahora el estilo se decide sólo con celdas numéricas, con el mismo filtro que usa
`numero()` para decidir si una celda tiene un número — de modo que lo que funda
el estilo es exactamente lo que después se va a leer con ese estilo.

Y el aviso de ambigüedad sólo sale cuando hay algo ambiguo. «Sin evidencia» no
significa que no se sepa: significa que ninguna celda trajo las dos formas a la
vez. En una planilla escrita con números enteros —que es la mitad de las
cargadas a mano— ninguna lectura cambia con el estilo, y el aviso mandaba a
revisar medidas que no tienen un decimal que revisar.

### A qué escala se le presenta la tabla al reconocedor

Dos cosas que costaban celdas, las dos medidas:

**Ampliar las imágenes chicas.** El tope decía «por debajo de mil píxeles no se
agranda: no hay información que recuperar». Lo primero es cierto y lo segundo no
viene al caso — el reconocedor tiene una altura de texto preferida, y
presentarle la misma información a esa altura no inventa nada. El guardarraíl se
activaba justo en el documento que más ayuda necesita, que mide exactamente 1000
px. Ahora se amplía, con tope (`MAX_AMPLIACION`), porque pasado el óptimo
interpolar sólo agrega borrosidad:

| se lee a | filas | cantidades |
|---|---|---|
| 1000 px (sin ampliar) | 9/14 | 8/14 |
| 1400 px | **13/14** | **12/14** |
| 2400 px | 11/14 | 10/14 |

**Recortarse a la tabla.** La imagen entera se llevaba al tamaño de trabajo
ANTES de buscar la tabla, así que una tabla que ocupa un tercio del cuadro se
quedaba con un tercio de los píxeles. La misma tabla dentro de un cuadro 2,5
veces más grande —una foto de lejos— no se leía: se caía sin reconocer el
encabezado. Ahora el lector se acerca él mismo, porque ya encontró la tabla con
las líneas de la regla, y pasa a leerla entera.

No hace falta pedirle a nadie que recorte la foto. Y el tope de ampliación es
**acumulado** desde el archivo original: interpolar lo interpolado no agrega
detalle y sí borrosidad — medido, 14 filas con una ampliación y 12 con dos para
el mismo tamaño final.

### La resolución es el límite, y está medido

Lo que decide si una planilla fotografiada se puede leer no es el tamaño de la
imagen: es cuántos píxeles tiene cada **columna** en el archivo original. Una
tabla de dos columnas en 1000 px se lee perfecto; una de diecisiete, no.

Medido sobre dos planillas reales —la piloto de 17 columnas y una de 14—
reducidas a varios anchos, contando cuántos valores se **pierden** y cuántos se
**cambian**:

| px por columna | perdidos | **cambiados** |
|---|---|---|
| ~105 | 0 | **0** |
| ~68 | 0 a 14 | **0** |
| ~48 | 21 a 32 | **0 a 1** |
| ~39 | 40 | **3** |

Perder celdas es tolerable: una medida en blanco se ve en la pantalla de
revisión, y encima el motor recalcula y no cierra contra lo que el papel afirma.
Cambiarlas no se ve por ningún lado.

#### Lo que apareció al mirar QUÉ se cambia

Un modo de falla que el umbral no describe. A ~52 px por columna el rótulo «b»
del encabezado se lee «d»: la columna de «b» queda rotulada «d» y la «d» de
verdad se descarta por repetida.

Eso, por sí solo, no movería ninguna medida de lugar. Lo que lo volvía grave era
que las medidas se armaban recorriendo las columnas **ordenadas por letra**, así
que el orden de los tramos pasaba a depender de haber leído bien un glifo. La
fila 13 de la planilla de 14 columnas —que en el papel dice 65, 155, 10— salía
**65, 10, 155**. Los tres suman lo mismo: la longitud cierra, el total cierra, el
peso cierra, y la única señal aparece en la obra con la barra doblada mal.

El arreglo es ordenar por **índice de columna**, que es el orden del papel. Con
eso, medido sobre dos planillas en seis anchos entre 1600 y 900 px: **ninguna
medida desordenada y ninguna cambiada, en ningún ancho**. Un rótulo mal leído
pasó a ser un cartel equivocado —se avisa— más la pérdida de la columna cuyo
rótulo quedó repetido, que el recálculo de la fila sí atrapa porque a las
medidas les falta un tramo.

#### Y de ahí los umbrales

**Entre 60 y 90 px por columna** la planilla se devuelve con un aviso de que van
a faltar celdas.

**Por debajo de 60** la pregunta ya no es el tamaño sino si la planilla se puede
verificar a sí misma. Una que declara su longitud y su total es su propio
oráculo: el importador recalcula la longitud desde las medidas y el total desde
la longitud por la cantidad por las veces, y la fila que no cierra queda
bloqueada a la vista. Así que se lee, con el aviso y el número.

Si NO trae esas columnas no hay contra qué recalcular, y ahí sí se niega:
`IMAGE_TOO_SMALL`, diciendo cuántos píxeles por columna hay, cuántos hacen falta
y qué columna faltó reconocer.

Negarse por resolución a secas fue lo primero que se implementó, y estaba mal:
tiraba planillas legibles —un documento de obra de 14 columnas a 51 px por
columna, que se lee con pérdidas— y dejaba a la persona con cero filas donde
podía tener la planilla entera para revisar.

Y el primer consejo, cuando existe el archivo, sigue siendo el mismo: **subir el
PDF**. Un PDF digital no tiene resolución; tiene texto.

### Qué tan bien lee una foto

Medido contra la propia planilla piloto, renderizada a imagen y leída por los
dos caminos:

| Entrada | Campos correctos | Celdas perdidas | **Valores mal leídos** |
|---|---|---|---|
| Escaneo limpio (200 dpi) | 150 / 150 | 0 | **0** |
| Foto derecha, con sombra y ruido | 131 / 150 | 19 | **0** |
| Girada 1,5°, JPEG 85, borrosa | 110 / 150 | 40 | **0** |
| Girada −3°, JPEG 75, borrosa | 96 / 150 | 54 | **0** |
| Girada 2,5°, JPEG 60, muy borrosa | 83 / 150 | 67 | **0** |

La última columna es la que importa. Lo que el reconocimiento hace cuando la
foto es mala es **perder** celdas, y una medida en blanco se ve sola en la
pantalla de revisión — encima el motor recalcula y no cierra contra lo que el
papel afirma. Una celda **mal leída** no se ve por ningún lado.

En las cinco, la estructura —cuántas filas hay y cómo se llaman— salió intacta:
la regla impresa es mucho más robusta que el texto, y por eso las celdas se
delimitan con ella y no agrupando el texto por coordenadas.

Lo que **no** se puede leer de una foto: una planilla sin recuadrar, una página
de continuación sola (el encabezado está sólo en la primera), una foto sacada
con más de unos pocos grados de inclinación, y una imagen por debajo del piso de
resolución de arriba.

### Lo que se probó y se descartó

Las tres cosas están anotadas en el código con el número que las descartó,
porque parecen buenas ideas y vuelven solas:

- **Elegir el canal de color en vez de luma.** Una planilla impresa en azul tiene
  el doble de contraste en el canal rojo (la tinta azul absorbe el rojo). Y no
  cambia una sola celda leída en una imagen limpia — y a baja resolución es
  **peor**: en el canal rojo la tinta queda casi negra y los trazos finos se
  rompen al reducir, mientras que en luma quedan gris medio y sobreviven.
- **Ampliar con Lanczos en vez de cúbica.** Recuperaba dos o tres celdas en una
  planilla de baja resolución, y en la foto degradada de la piloto hacía que un
  «6» se leyera «8». Ganar celdas a cambio de cambiar un valor es el
  intercambio que este módulo no acepta.
- **Releer las celdas vacías de las columnas casi completas.** No recuperó una
  sola celda útil en ninguno de los documentos de prueba, y el riesgo es
  asimétrico. La relectura quedó sólo para el ENCABEZADO, donde un rótulo sin
  leer pierde una columna entera y donde equivocarlo no produce un dato falso
  sino una columna «no reconocida», a la vista.
- **Rechazar la imagen por resolución, y rechazarla por un rótulo incoherente.**
  Las dos se implementaron y las dos se sacaron, porque tiraban documentos que
  se leen bien: la primera un documento de obra real, la segunda ése y dos fotos
  degradadas de la piloto. El modo de falla que las motivaba era real; lo que
  estaba mal era el remedio. Se arregló en la causa —el orden de las medidas— y
  las dos pasaron a ser avisos.

No interpreta el croquis. Los dibujos son trazos, y deducir de ellos la
geometría de doblado sería inventar una instrucción para el armador. Lo que sí
lee son las **letras** del croquis, que son texto de verdad: cuántas letras
distintas hay es cuántas medidas usa la figura.

Los valores viajan **en la unidad leída**, con la unidad detectada (`unit`) y la
evidencia con la que se detectó (`unit_evidence`) al lado. La conversión a
metros pasa en `naau-server`, en el mismo borde donde ya se convierte todo lo
demás — y así la persona puede corregir la unidad antes de que se convierta
nada.

## Cómo se levanta

Una sola vez, para crear el entorno:

```bash
cd naau-worker
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt   # Linux: .venv/bin/python
.venv/Scripts/python -m pip install --force-reinstall --no-deps opencv-python-headless==4.10.0.84
```

La segunda línea **no es opcional**: `rapidocr-onnxruntime` declara
`opencv-python`, que es la variante con ventanas de escritorio y necesita
`libGL` en Linux. Las dos publican el mismo paquete `cv2`, así que hay que
forzar la que no abre ventanas — si no, el worker arranca en Windows y falla al
importar `cv2` en un Linux de servidor, que es donde nadie lo está mirando.

Los modelos de OCR (15 MB) viajan **dentro** del paquete: no se descarga nada la
primera vez que alguien sube una foto, que en un servidor sin internet de salida
es la diferencia entre que funcione y que no.

`openpyxl` entró con la plantilla de Excel y está en `requirements.txt`, así que
la primera línea lo instala. En un entorno que ya existía hay que volver a
correrla, o `pip install openpyxl==3.1.5` a secas — se importa **diferido**, así
que un worker con las dependencias a medio instalar arranca igual y falla recién
cuando alguien sube el primer `.xlsx`, con `EXCEL_UNAVAILABLE`.

Después, **no hace falta levantarlo a mano**: `npm run dev` en `naau-server`
arranca los dos procesos y le pasa a éste el token y el puerto derivados del
`.env` del server. Ver `naau-server/scripts/dev.ts`.

Eso resuelve el desencuentro más caro de este par de procesos: el token escrito
en dos archivos, rotado en uno y no en el otro. El síntoma sería un `401` entre
dos procesos propios, que manda a buscar el problema a la autenticación de
usuarios —que no tiene nada que ver—. Con el arranque conjunto hay una sola
fuente, y si no hay ninguna se genera un token efímero que muere con la terminal.

Suelto también anda, que es como lo va a arrancar un supervisor en producción:

```bash
export NAAU_WORKER_TOKEN=...        # el mismo que el del server, mínimo 32 caracteres
.venv/Scripts/python main.py
```

Escucha en `127.0.0.1:8099` (`NAAU_WORKER_PORT` lo cambia).

### Las dos guardas, y por qué las dos

- **Sólo loopback**, porque este proceso no tiene noción de identidad. Expuesto
  sería CPU gratis para cualquiera en el servidor de producción.
- **Token igual**, porque «loopback» no es «privado»: en la misma máquina hay
  otros procesos y, en un servidor compartido, otros usuarios del sistema.

El proceso **se niega a arrancar** sin token, o con uno de menos de 32
caracteres. Un default vacío parece protección y no lo es.

### Sobre el timeout

No hay timeout dentro de este proceso, y es deliberado: matar un hilo de Python
a mitad de camino deja el intérprete en un estado que no se puede razonar, así
que sería un timeout de mentira. El que importa es el del cliente HTTP en
`naau-server`, que es quien no puede quedarse esperando. Lo que garantiza acá
que el trabajo termine es que el tamaño (8 MB) y las páginas (20) están
acotados.

## Las pruebas

```bash
.venv/Scripts/python -m pytest
```

La planilla piloto (`../PLANILLA_VIGAS ENTRE PISO.pdf`) es un documento de obra
real, y la aserción más fuerte de la suite no es «hay 27 filas»: es que las
cuatro relaciones que la planilla afirma de sí misma —el parcial es la suma de
las medidas, el total es el parcial por la cantidad, la pérdida es el total por
1,02 y el peso es la longitud con pérdida por el peso unitario— **cierran en las
27 filas**. Si una celda se leyera de la columna vecina, o un `6,150.00` se
leyera como 6,15, esas cuentas no dan. Es la planilla haciendo de oráculo de su
propia lectura.

Si el archivo no está, esas pruebas se saltean; las de las piezas sueltas
corren igual.

La lectura de imágenes se prueba con **la misma planilla**, renderizada a
imagen: la referencia es `parse_pdf` sobre el archivo original, donde no hay
nada que reconocer. Un OCR no se puede probar contra valores escritos a mano en
el test —quien los escribe los copia del documento, y entonces el test dice «el
OCR leyó lo que yo leí»—. Y lo que esas pruebas exigen no es un porcentaje de
acierto sino que **ni un solo valor leído sea distinto del que dice el papel**,
incluso sobre cuatro degradaciones que imitan una foto de celular.

Tardan un minuto largo: cargan los modelos ONNX y leen cinco imágenes. Si
faltan las dependencias de OCR, se saltean.

La deducción de columnas (`tests/test_column_roles.py`) usa las filas de la
planilla de obra de 14 columnas por la misma razón: el papel es su propio
oráculo. Y las prueba **desordenadas** —ocho barajados al azar— porque sin el
rótulo la posición no significa nada, así que si el reparto dependiera del orden
alguno de esos barajados lo tendría que delatar.

El Excel (`tests/test_xlsx.py`) se prueba de ida y vuelta: se genera la
plantilla oficial, se llena con celdas y se vuelve a leer. La prueba que importa
es que **ninguna columna de la plantilla propia quede sin reconocer** — es lo
único que impide que la plantilla y el lector se separen, y sin ella el archivo
se seguiría descargando igual de lindo. Son milisegundos y no necesitan OCR.

### Cuáles son livianas y cuáles no

```bash
# Rápidas (segundos): aritmética, mapeo de columnas, Excel, API
.venv/Scripts/python -m pytest tests/test_deduccion_de_celdas.py tests/test_column_roles.py \
    tests/test_pdf_parser.py tests/test_planilla_dos_pisos.py tests/test_xlsx.py

# Con OCR (un minuto largo, cargan los modelos ONNX)
.venv/Scripts/python -m pytest tests/test_image_table.py tests/test_imagen_dos_pisos.py
```
