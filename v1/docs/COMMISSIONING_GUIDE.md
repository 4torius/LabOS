# LabOS — Guida alla Messa in Opera del Sistema

Questo documento è la lista di controllo per commissioning fisico del laboratorio. Seguire ogni sezione in ordine prima di eseguire il primo workflow reale.

---

## 1. Prerequisiti Software

### 1.1 Dipendenze
- Python 3.10+ installato e nel PATH
- .NET 6+ installato (per il server Tecan)
- Venv attivo (`LabOS\.venv`)

```powershell
cd LabOS
.\.venv\Scripts\Activate.ps1
cd v1
pip install -r requirements.txt
```

### 1.2 Verifica stubs gRPC
Se i file `.proto` sono stati modificati, rigenerare gli stubs prima di avviare:
```bash
python regen_stubs.py
```

---

## 2. Rete e Connettività

### 2.1 Opentrons Flex
| Parametro | Valore |
|-----------|--------|
| IP robot | `169.254.161.83` (link-local, USB/diretta) |
| Porta API REST | `31950` |
| Verifica | `curl http://169.254.161.83:31950/health` → deve rispondere `{"name": "OT-3 ..."}` |

- Il PC deve essere collegato al robot via USB-C oppure via switch di rete dedicato.
- Se l'IP cambia, aggiornare `SiLA2/OpentronsSiLA2Server/config.yaml` → `robot_ip`.

### 2.2 Tecan Infinite M200 Pro
| Parametro | Valore |
|-----------|--------|
| Connessione | USB (COM) o Ethernet — configurato in iControl |
| Server SiLA2 | `localhost:50051` (avviato dal launcher) |
| SDK COM | Solo Windows — iControl deve essere installato |

- iControl deve essere **chiuso** prima di avviare il server SiLA2 (il server prende il lock COM).
- Se iControl è aperto, il server Tecan fallisce all'avvio con `REGDB_E_CLASSNOTREG` o simile.

### 2.3 Robot Mobile (AMR)
| Parametro | Valore |
|-----------|--------|
| IP robot | `192.168.11.22` (rete LAN laboratorio) |
| Porta SiLA2 | `50053` |
| ROS Master | `http://localhost:11311` (su PC di bordo del robot) |

- Il robot deve essere acceso e sul wifi/LAN corretto prima di avviare il launcher.
- Se assente, impostare `enabled: false` in `lab_config.yaml` → `servers.mobile` per evitare errori di connessione.

### 2.4 Manual Station
- Server locale, avviato automaticamente. Nessuna configurazione hardware richiesta.

---

## 3. Configurazione HAL (Hardware Abstraction Layer)

Il file HAL descrive la disposizione fisica di labware, pipette e moduli sull'Opentrons Flex. **Deve corrispondere esattamente a ciò che è montato sul deck fisico.**

### 3.1 File HAL disponibili
In `Library/HardwareConfig/`:
- `Standard_Flex_Setup.json` — setup generico con 1 pipetta, piastra, reservoir e tip rack
- `SerialDilution_Setup.json` — setup per diluizioni seriali
- `Generic_HAL.json` — base minimale

### 3.2 Come creare o modificare un HAL

Aprire il file e verificare/adattare ogni campo:

```json
{
  "ConfigName": "NomeSetup",
  "Description": "Descrizione breve",
  "Modules": {
    "HeaterShaker": { "Type": "heaterShakerModuleV1", "Slot": "A1" }
  },
  "Trash": {
    "MyTrash": { "Type": "TrashBin", "Slot": "A3" }
  },
  "Labware": {
    "MyTips":      { "LoadName": "opentrons_flex_96_filtertiprack_1000ul", "Slot": "B3" },
    "MyReservoir": { "LoadName": "nest_1_reservoir_290ml",                 "Slot": "C2" },
    "MyPlate":     { "LoadName": "corning_24_wellplate_3.4ml_flat",        "Slot": "D2" }
  },
  "Pipettes": {
    "left": "flex_1channel_1000"
  }
}
```

**Regole:**
- `Slot` deve corrispondere alle posizioni fisiche sul deck (A1–D4 per Flex).
- `LoadName` deve essere un nome labware Opentrons valido (vedi sezione 5).
- I nomi logici (`MyTips`, `MyPlate`, ecc.) devono coincidere con quelli usati nelle ricette JSON.
- Se un modulo non è presente fisicamente, **rimuovere** la voce da `Modules`.

### 3.3 Associare HAL a una ricetta
Ogni ricetta JSON (`Library/Recipes/*.json`) referenzia i nomi logici del HAL. Il HAL viene selezionato al momento dell'esecuzione via parametro `hal_config` nel workflow o nel form della dashboard.

---

## 4. Piastre — Definizione del Set di Lavoro

Le piastre devono essere definite una volta sola e registrate in tre posti:

| Dove | Scopo |
|------|-------|
| `Library/Labware/Plates/*.plate.json` | Catalogo LabOS (nome logico, dimensioni, note) |
| Opentrons HAL (`LoadName`) | Nome labware Opentrons (vedi lista sotto) |
| Tecan iControl (`Plate Type`) | Nome piastra in iControl per il metodo `.mdfx` |

### 4.1 Procedura di definizione (da fare con il team di laboratorio)

Per ogni piastra usata in laboratorio:

1. **Nome logico** — scegliere un nome univoco leggibile (es. `Corning_96F_TC`, `Greiner_384_PS`)
2. **Nome Opentrons** — trovare il `loadName` corretto:
   - WebApp → Recipe Generator → sezione Plate Catalog (filtrare per tipo)
   - Oppure: `python -c "from opentrons.protocol_api import labware; print(labware.list_definition_loadnames())"`
   - Oppure cercare nella directory: `LabOS\.venv\Lib\site-packages\opentrons\shared_data\labware\definitions\2\`
3. **Nome Tecan iControl** — aprire iControl → Method Editor → selezionare il metodo → Plate Type dropdown → annotare il nome esatto (es. `GRE96ft`, `GRE96ft_Corning360`)
4. **Creare il file `.plate.json`** in `Library/Labware/Plates/`:

```json
{
  "id": "Corning_96F_TC",
  "display_name": "Corning 96-well Flat Bottom TC",
  "format": "SBS96",
  "rows": 8,
  "columns": 12,
  "well_volume_ul": 360,
  "manufacturer": "Corning",
  "catalog_number": "3596",
  "opentrons_load_name": "corning_96_wellplate_360ul_flat",
  "tecan_plate_name": "GRE96ft_Corning360",
  "instruments": ["opentrons", "tecan"],
  "notes": ""
}
```

### 4.2 Set minimo consigliato per avvio laboratorio

Concordare almeno questi formati con il team:

| Piastra | Formato | Opentrons loadName | Tecan iControl name |
|---------|---------|-------------------|---------------------|
| Piastra 96 flat bottom standard | SBS96 | `corning_96_wellplate_360ul_flat` | `GRE96ft` |
| Piastra 96 con trattamento TC | SBS96 | `corning_96_wellplate_360ul_flat` | `GRE96ft_Corning360` |
| Deep well 96 | SBS96 | `nest_96_wellplate_2ml_deep` | (verificare) |
| Reservoir 290mL | N/A | `nest_1_reservoir_290ml` | N/A (solo Opentrons) |

> **Nota:** il campo `tecan_plate_name` nel `.plate.json` è documentale per ora. Per cambiare il tipo di piastra in un run Tecan, usare il parametro `PlateType` nel passo workflow `RunMeasurement`.

---

## 5. Protocolli di Misura Tecan (`.mdfx`)

I file `.mdfx` sono i protocolli di misura creati con iControl. Devono essere copiati in `Library/Analysis/`.

### 5.1 Creare un protocollo in iControl

1. Aprire iControl → Method Editor
2. Selezionare il tipo di misura (Absorbance, Fluorescence, ecc.)
3. Configurare lunghezze d'onda, gain, agitazione, ecc.
4. **Impostare il tipo di piastra** nel protocollo (questa è la "piastra default" del metodo)
5. Salvare come `.mdfx`
6. Copiare il file in `Library/Analysis/`

### 5.2 Override del tipo di piastra da workflow
Se si vuole usare lo stesso protocollo `.mdfx` con piastre diverse, aggiungere il parametro `PlateType` nel passo `RunMeasurement` del workflow builder. Il server Tecan logga l'override ma non modifica il `.mdfx` (il cambio è gestito dall'operatore o da future integrazioni).

---

## 6. Ricette Opentrons (`.json`)

Le ricette descrivono i movimenti liquidi in formato JSON interpretato dal server Opentrons.

### 6.1 Creare una ricetta

Usare la WebApp: **Recipe Generator** (`/recipe-generator`) oppure creare manualmente il JSON:

```json
{
  "name": "MiaRicetta",
  "description": "Trasferimento 100µL da reservoir a piastra",
  "hal_config": "Standard_Flex_Setup",
  "steps": [
    {
      "action": "transfer",
      "volume": 100,
      "source": { "labware": "MyReservoir", "wells": ["A1"] },
      "destination": { "labware": "MyPlate", "wells": ["A1","B1","C1"] },
      "pipette": "left",
      "tip_strategy": "once"
    }
  ]
}
```

**Regole:**
- Salvare il file in `Library/Recipes/` con estensione `.json`
- **Usare UTF-8 senza BOM** — se si salva con VSCode o Notepad++, verificare che la codifica sia `UTF-8` (non `UTF-8 with BOM`)
- I nomi di labware (`MyReservoir`, `MyPlate`, ecc.) devono corrispondere esattamente al HAL selezionato

### 6.2 Validare una ricetta
Prima di eseguire fisicamente, usare la WebApp → Dashboard → comando `ValidateRecipe` sull'Opentrons, oppure direttamente:
```bash
python -c "
import asyncio, sys
sys.path.insert(0, 'src')
from lab_core import LabCore
async def main():
    core = LabCore()
    await core.initialize()
    r = await core.execute_command('opentrons', 'ValidateRecipe', {'recipe_name': 'MiaRicetta', 'hal_config': 'Standard_Flex_Setup'})
    print(r)
asyncio.run(main())
"
```

---

## 7. Workflow

I workflow orchestrano più strumenti in sequenza (o in parallelo).

### 7.1 Creare un workflow

Usare la WebApp: **Workflow Builder** (`/workflow-builder`), oppure creare manualmente in `Library/Workflows/`:

```json
{
  "name": "PlateRead_Completo",
  "description": "Prepara piastra con Opentrons e legge con Tecan",
  "steps": [
    {
      "id": "step_1",
      "name": "Pipettamento",
      "instrument": "opentrons",
      "command": "ExecuteRecipe",
      "parameters": {
        "recipe_name": "MiaRicetta",
        "hal_config": "Standard_Flex_Setup"
      }
    },
    {
      "id": "step_2",
      "name": "Lettura piastra",
      "instrument": "tecan",
      "command": "RunMeasurement",
      "parameters": {
        "protocol_file": "Absorbance_450nm.mdfx"
      },
      "depends_on": ["step_1"]
    }
  ]
}
```

### 7.2 PlateID — propagazione automatica
- Dopo `ExecuteRecipe`, LabOS genera automaticamente un `plate_id` (`PLATE_<timestamp>_<recipe>`)
- Il `plate_id` viene iniettato automaticamente nel passo successivo Tecan `RunMeasurement`
- Non è necessario configurarlo manualmente nel workflow

---

## 8. Avvio del Sistema

### 8.1 Sequenza di avvio consigliata

1. Accendere gli strumenti fisici (Opentrons Flex, Tecan)
2. Collegare i cavi USB/rete
3. Attendere che il robot Opentrons sia in stato `idle` (LED bianco fisso)
4. Verificare che iControl sia **chiuso**
5. Attivare il venv e avviare il launcher:

```powershell
cd LabOS
.\.venv\Scripts\Activate.ps1
cd v1
python launcher.py --all
```

6. Aprire il browser: `http://127.0.0.1:5000`
7. Verificare nella dashboard che tutti gli strumenti configurati appaiano in verde

### 8.2 Verifica connessioni

Dalla dashboard → **Instruments**: ogni server deve mostrare stato "Online". Se un server è rosso:

| Strumento | Causa frequente | Soluzione |
|-----------|----------------|-----------|
| Opentrons | Robot non raggiungibile | Verificare IP e connessione USB |
| Tecan | iControl aperto o SDK non installato | Chiudere iControl, verificare COM |
| Mobile | Robot spento o IP diverso | Impostare `enabled: false` se non usato |

---

## 9. Primo Run — Checklist

- [ ] HAL file creato e verificato contro il deck fisico
- [ ] Almeno una ricetta salvata come `UTF-8 senza BOM`
- [ ] Almeno un protocollo `.mdfx` copiato in `Library/Analysis/`
- [ ] Tipo di piastra nel `.mdfx` corrisponde alla piastra fisica
- [ ] Piastre e tip rack caricati nelle posizioni corrette del deck
- [ ] Tutti i server sono "Online" nella dashboard
- [ ] Test manuale del comando `SetLights` su Opentrons dalla dashboard (verifica connessione gRPC)
- [ ] Test manuale del comando `GetStatus` su Tecan dalla dashboard
- [ ] `ValidateRecipe` eseguito senza errori
- [ ] Primo workflow eseguito in modalità "step-by-step" (supervisore presente)

---

## 10. Risoluzione Problemi Frequenti

| Errore | Causa | Soluzione |
|--------|-------|-----------|
| `Unexpected UTF-8 BOM` | File JSON salvato con BOM | Risalvare come UTF-8 (senza BOM) in VSCode o Notepad++ |
| `bad argument type for built-in operation` | Parametro tipo sbagliato | Verificare che i parametri boolean siano `true`/`false` e non stringhe |
| Tecan result come popup lungo | Risposta AnIML binaria nel log | Normale — il log è troncato a 200 caratteri per design |
| Workflow si ferma al primo step | Errore nel primo step | Aprire il log del workflow, controllare il messaggio di errore del passo |
| `REGDB_E_CLASSNOTREG` (Tecan) | iControl non installato o 32/64-bit mismatch | Verificare installazione iControl, riavviare il server |
| Robot non raggiungibile | IP cambiato o cavo scollegato | Verificare IP con `curl http://<ip>:31950/health` |

---

## 11. Contatti e Riferimenti

- **Documentazione tecnica completa**: `v1/docs/SYSTEM_DOCUMENTATION.md`
- **Aggiungere un nuovo strumento**: `v1/SiLA2/ADDING_INSTRUMENTS.md`
- **Ricette e HAL**: `v1/docs/OPENTRONS_RECIPES_HAL.md`
- **Protocolli Tecan**: `v1/docs/TECAN_SERVER.md`
- **Issues e bug**: https://github.com/anthropics/claude-code/issues (solo per Claude Code — per LabOS aprire issue nel repository di progetto)
