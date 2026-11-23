# Aggiungere una piastra (Opentrons e Tecan)

Questa guida spiega come aggiungere una piastra a LabOS in modo che si possa
usare sia sull'Opentrons (pipettare) sia sul Tecan (misurare). Vale anche per
le piastre custom.

## La regola: un solo nome

Una piastra ha **un nome**, per esempio `euroclone_48_wellplate_1500ul`, e
quel nome compare identico ovunque:

| Dove | Campo |
|---|---|
| `Library/Labware/Plates/<nome>.plate.json` | nome del file e `"id"` |
| stesso file | `"opentrons_load_name"` |
| HAL e ricette | `"LoadName"` |
| `Library/Labware/Reservoirs/<nome>.json` (solo piastre custom) | nome del file e `parameters.loadName` |
| `RunMeasurement` del Tecan | parametro `PlateType` |

Se i nomi non coincidono, LabOS non capisce che è la stessa piastra: i pozzetti
della ricetta non trovano le dimensioni, il database piastre non la riconosce,
il Tecan non la offre. Usa solo lettere minuscole, cifre e `_`.

## I file in gioco

| File | A cosa serve | Quando serve |
|---|---|---|
| `Library/Labware/Plates/<nome>.plate.json` | Scheda LabOS della piastra: righe, colonne, volumi, dimensioni. La leggono ricette, Excel, database piastre, editor HAL e Tecan. | **Sempre** |
| `Library/Labware/Reservoirs/<nome>.json` | Definizione Opentrons completa (posizione di ogni pozzetto). Il server Opentrons la carica sul robot prima del run. | Solo se l'Opentrons **non** conosce già la piastra |
| `C:\Users\Public\Documents\Tecan\Pdfx\<codice>.pdfx` | Definizione della piastra per il lettore Tecan (dove sono i pozzetti). | Solo per misurare col Tecan |

Il collegamento col Tecan è un campo nella scheda LabOS:

```json
"tecan_plate_name": "GRE96ft"
```

`GRE96ft` è il nome del file `.pdfx` senza estensione. Quando una misura chiede
questa piastra, il bridge Tecan apre il protocollo `.mdfx` e sostituisce la
piastra scritta lì dentro con `GRE96ft`. Il file `.mdfx` su disco non viene
modificato. Il lettore va poi a cercare `GRE96ft` nella sua libreria interna.

Una piastra **senza** `tecan_plate_name` resta usabile sull'Opentrons, ma non
compare nel menu "Plate type" di `RunMeasurement`.

---

## Passo 1 — La scheda LabOS (sempre)

Crea `Library/Labware/Plates/<nome>.plate.json`. Conviene copiare una piastra
simile e cambiare i valori:

```json
{
  "id": "euroclone_48_wellplate_1500ul",
  "display_name": "EuroClone 48 Well Plate 1.5 mL",
  "category": "wellPlate",
  "format": "48Standard",
  "rows": 6,
  "columns": 8,
  "total_wells": 48,
  "row_labels": ["A","B","C","D","E","F"],
  "max_volume_ul": 1500,
  "working_volume_ul": 800,
  "well_shape": "circular",
  "well_diameter_mm": 10.6,
  "well_depth_mm": 17.5,
  "dimensions_mm": {"x": 127.6, "y": 85.4, "z": 20.2},
  "bottom_type": "flat",
  "instruments": ["opentrons", "tecan"],
  "opentrons_load_name": "euroclone_48_wellplate_1500ul",
  "tecan_compatible": true,
  "tecan_plate_name": "GRE48ft",
  "brand": "EuroClone",
  "catalog_numbers": ["ET3048"],
  "notes": "Da dove vengono le misure (scheda tecnica, calibro, ...)."
}
```

- Il file deve chiamarsi `<nome>.plate.json`. I file con altri suffissi (per
  esempio `.plate_CUSTOM.json`) vengono ignorati da tutto il sistema: vanno bene
  per bozze, non per piastre vere.
- `rows` e `columns` sono obbligatori per il Tecan: il bridge li confronta con
  il `.pdfx`.
- `tecan_plate_name` e `tecan_compatible` servono solo se la piastra va
  misurata (Passo 3).

## Passo 2 — L'Opentrons

### 2a. La piastra è nel catalogo Opentrons

Molte piastre commerciali sono già nel catalogo Opentrons
(`corning_96_wellplate_360ul_flat`, `nest_96_wellplate_200ul_flat`, ...).
In quel caso **usa come nome proprio il load name Opentrons** e non serve
nient'altro: il robot la conosce già.

### 2b. Piastra custom

Serve la definizione completa, con la posizione di ogni pozzetto:

1. Crea la definizione con **Opentrons Labware Creator**
   (<https://labware.opentrons.com/create>) usando le misure della scheda tecnica
   del produttore, controllate col calibro. Ne esce un file JSON.
2. Nel file, imposta `parameters.loadName` uguale al nome della piastra.
   `namespace` **non** deve essere `"opentrons"`: va bene quello che mette il
   Creator (`custom_beta`) oppure `labos_custom`.
3. Salvalo come `Library/Labware/Reservoirs/<nome>.json`. La cartella si chiama
   "Reservoirs" anche per le piastre: è l'unica in cui il server Opentrons
   cerca le definizioni complete.
4. Prima del run il server Opentrons carica sul robot ogni definizione con
   `namespace` diverso da `opentrons` che la ricetta usa.

> **Attenzione:** il robot usa **queste** coordinate per muovere la pipetta.
> Coordinate stimate possono mandarla fuori dal pozzetto. Prima di pipettare
> davvero, fai una prova a vuoto e controlla la posizione su A1 e sull'ultimo
> pozzetto.

## Passo 3 — Il Tecan

### 3a. Trova un `.pdfx` che descrive la stessa piastra

Le definizioni installate sono in `C:\Users\Public\Documents\Tecan\Pdfx`
(circa 140 file). Il nome segue lo schema produttore + pozzetti + fondo:
`COS` Corning, `GRE` Greiner, `NUN` Nunc, `BD` Falcon; `ft` fondo piatto
trasparente, `fb` fondo nero, `fw` fondo bianco. Esempi: `COS24ft`, `GRE96ft`,
`COS48ft`.

Ogni `.pdfx` è un file XML leggibile. Aprilo e controlla:

- `comment="Cat. No.: ..."` deve contenere il codice catalogo della tua piastra.
  È il controllo migliore: per esempio `GRE96ft` riporta 655101/655161, gli
  stessi codici di `greiner_96_wellplate_382ul`.
- In `DefinitionRectanglePlateBaseDimensions` ci sono le misure, in
  **micrometri** (`127760` = 127,76 mm).

Se il codice catalogo corrisponde, metti il nome del file in
`"tecan_plate_name"` e vai al punto 3c.

Se nessun `.pdfx` riporta il tuo codice catalogo, confronta le misure (punto
3b). Se coincidono entro qualche decimo di millimetro puoi usare quel `.pdfx`.
Altrimenti creane uno nuovo.

### 3b. Creare un `.pdfx` nuovo (piastra custom)

Su questo PC (i-control 2.0) non c'è un editor delle definizioni piastra.
Si parte quindi da un `.pdfx` esistente simile:

1. Copia in `C:\Users\Public\Documents\Tecan\Pdfx` il `.pdfx` più simile (stesso
   numero di pozzetti, stessa forma) e dagli un nome nuovo, per esempio
   `EUR48ft.pdfx`.
2. Nel file nuovo cambia:
   - `TecanPlateDefinition name="..."`: deve essere **uguale al nome del file**
     (senza `.pdfx`);
   - `guid="..."`: un GUID nuovo (in PowerShell: `[guid]::NewGuid()`), così non
     ne duplica un altro;
   - `comment`, `manufacturer`: produttore e codice catalogo;
   - le misure, in micrometri (tabella sotto).
3. Lascia com'è la sezione `DefinitionWasherBaseSettings`: riguarda il lavatore,
   non il lettore.

| Campo `.pdfx` | Significato | Dalla definizione Opentrons (mm × 1000) |
|---|---|---|
| `outsideX`, `outsideY` | Ingombro esterno della piastra | `dimensions.xDimension`, `dimensions.yDimension` |
| `firstColumnDistanceX` | Dal bordo sinistro al centro di A1 | `wells.A1.x` |
| `firstColumnDistanceY` | Dal bordo **posteriore** (riga A) al centro di A1 | `yDimension − wells.A1.y` |
| `lastColumnDistanceX` | Dal bordo sinistro al centro dell'ultima colonna | `x` dell'ultimo pozzetto |
| `lastColumnDistanceY` | Dal bordo posteriore al centro dell'ultima riga | `yDimension − y` dell'ultimo pozzetto |
| `nrOfRows`, `nrOfColumns` | Righe e colonne | `rows`, `columns` della scheda LabOS |
| `height` | Altezza della piastra | `dimensions.zDimension` |
| `dimensionX`, `dimensionY` | Diametro del pozzetto in alto | `diameter` |
| `dimensionBottomX`, `dimensionBottomY` | Diametro sul fondo | dalla scheda tecnica |
| `dimensionZ` | Profondità del pozzetto | `depth` |
| `maxCapacity` | Volume massimo (µL, non µm) | `totalLiquidVolume` |

La Y è l'unico punto delicato. Opentrons misura la Y dal bordo **anteriore**,
il Tecan dal bordo **posteriore**: per questo si sottrae da `yDimension`.
Controprova su una piastra nota: Corning 96 ha A1 a `y = 74,24` su
`yDimension = 85,47`, quindi `85,47 − 74,24 = 11,23 mm`. `GRE96ft` riporta
`firstColumnDistanceY = 11240`.

> Una definizione `.pdfx` sbagliata **non dà errore**: il lettore misura
> semplicemente nel punto sbagliato. Il bridge controlla solo che il file
> esista e che righe e colonne coincidano. Il resto va verificato con una
> misura (punto 3d).
>
> Una `.pdfx` creata a mano non è ancora stata provata in questo laboratorio.
> La prima volta segui il punto 3d con attenzione.

### 3c. Il range dei pozzetti (di solito non serve)

Il bridge calcola da solo il range completo da `rows` e `columns`: 6 × 8
diventa `A1:F8`. Se un `.mdfx` salvato dal software Tecan per quella piastra
riporta un range diverso in `<PlateRange range="...">`, copia quel valore nella
scheda LabOS:

```json
"tecan_plate_range": "A1~D6"
```

Oggi serve solo per la Corning 24, per cui i protocolli Tecan riportano
`A1~D6`.

### 3d. Riavvia e verifica

1. **Riavvia il server Tecan.** Il bridge legge le piastre una volta sola,
   all'avvio. Nel log di avvio trovi:
   - `[PlateTypeLibrary] N piastre misurabili da ...: <elenco>`: la piastra deve
     esserci;
   - `Piastra '<nome>' scartata: <motivo>`: `.pdfx` non trovato, righe/colonne
     diverse, `id` duplicato, ...
2. In Workflow Builder o dalla dashboard, `RunMeasurement` deve offrire la
   piastra in "Plate type". Nell'editor HAL le piastre misurabili hanno ✅.
3. Fai una misura di prova: liquido colorato solo in **A1** e nell'**ultimo
   pozzetto**, assorbanza. Nei risultati il segnale alto deve comparire
   esattamente in quei due pozzetti. Se compare in un pozzetto vicino, o è più
   basso del previsto, le coordinate del `.pdfx` sono da correggere.

---

## Esempio: EuroClone Primo 48 (ET3048)

In LabOS ci sono due piastre EuroClone 48. Restano **distinte** perché le misure
sono diverse:

| Nome | Ingombro | Volume pozzetto | Definizione Opentrons |
|---|---|---|---|
| `euroclone_48_wellplate_1500ul` | 127,6 × 85,4 mm | 1500 µL | nessuna in `Reservoirs/` |
| `custom_fra_euroclone_primo_48_wellplate_1.5ml_flat` | 127,2 × 85,0 mm | 1000 µL | `Reservoirs/custom_fra_...json` (usata dagli HAL) |

Entrambe hanno `"tecan_plate_name": "GRE48ft"`: due piastre LabOS possono usare
la stessa definizione Tecan.

Perché `GRE48ft`:

- Le coordinate della custom sono segnate come **stime non verificate** (vedi il
  campo `notes` della definizione Opentrons).
- Nessun `.pdfx` installato riporta ET3048. I tre da 48 pozzetti, con le misure
  stimate a confronto (mm):

  | | A1 X | A1 Y (da dietro) | ultima colonna X | ultima riga Y |
  |---|---|---|---|---|
  | EuroClone (stima) | 17,5 | 10,6 | 108,5 | 75,6 |
  | `GRE48ft` | 18,38 | 10,24 | 109,38 | 75,24 |
  | `COS48ft` | 18,16 | 10,08 | 109,72 | 75,48 |
  | `NUN48ft` | 16,6 | 9,1 | 111,1 | 76,6 |

  `GRE48ft` è il più vicino: stesso passo di 13 mm, circa 0,9 mm di
  differenza su un pozzetto largo 10,6 mm. Però la differenza è calcolata su
  **stime**. Fai la misura di prova del punto 3d. Se il segnale non è
  centrato, prendi le quote vere (calibro o scheda tecnica EuroClone) e crea un
  `EUR48ft.pdfx` (punto 3b).

## Perché il `.pdfx` non si genera in automatico

Dalla definizione Opentrons si potrebbero calcolare quasi tutti i campi del
`.pdfx` (la tabella al punto 3b fa proprio questo). Non lo facciamo in
automatico perché:

- un `.pdfx` sbagliato non dà errore, misura nel punto sbagliato: un file
  generato da misure stimate sembrerebbe funzionare;
- alcuni campi (altezza con coperchio, diametro sul fondo) non sono nella
  definizione Opentrons;
- nessun `.pdfx` creato fuori dal software Tecan è ancora stato provato su
  questo lettore.

Il controllo automatico c'è già: il bridge rifiuta un `tecan_plate_name` che
non esiste o che ha righe/colonne diverse dalla piastra. Quando avremo provato
con successo un `.pdfx` fatto a mano, si potrà aggiungere un generatore.
