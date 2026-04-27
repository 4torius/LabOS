# Opentrons Protocol Generator - Guida alla Manutenzione

> **Politica**: Aggiornare solo quando strettamente necessario (nuove funzionalità richieste, bug critici, deprecazioni forzate).

## Indice

1. [Architettura del Sistema](#architettura-del-sistema)
2. [Struttura del File](#struttura-del-file)
3. [Aggiungere un Nuovo Comando](#aggiungere-un-nuovo-comando)
4. [Aggiornare per Nuova Versione API](#aggiornare-per-nuova-versione-api)
5. [Aggiungere Nuovo Hardware](#aggiungere-nuovo-hardware)
6. [Troubleshooting](#troubleshooting)
7. [Testing](#testing)
8. [Riferimenti API Opentrons](#riferimenti-api-opentrons)

---

## Architettura del Sistema

```
┌─────────────────┐     ┌──────────────────┐     ┌─────────────────┐
│   JSON Recipe   │────▶│ RecipeValidator  │────▶│ ProtocolGenerator│
│   (da utente)   │     │  (validazione)   │     │   (genera .py)   │
└─────────────────┘     └──────────────────┘     └─────────────────┘
                                                          │
                        ┌──────────────────┐              │
                        │ProtocolSimulator │◀─────────────┘
                        │(test locale)     │
                        └──────────────────┘
                                 │
                        ┌──────────────────┐
                        │  Opentrons Flex  │
                        │  (esecuzione)    │
                        └──────────────────┘
```

**Perché questo design?**

L'API HTTP di Opentrons supporta solo ~15 comandi base. Per operazioni complesse (thermocycler profiles, liquid classes, nozzle configuration) è necessario caricare un file `.py`. Il UNIVERSAL_TEMPLATE è un protocollo Python che interpreta le impostazioni JSON a runtime.

---

## Struttura del File

**File**: `SiLA2/OpentronsSiLA2Server/src/protocol_generator.py`

```
Linee 1-25      │ Imports e logging
Linee 26-100    │ Dataclasses (ValidationError, SimulationResult, GenerationResult)
Linee 103-177   │ COMMAND_SCHEMA - Schema di validazione per ogni comando
Linee 180-196   │ Costanti (VALID_MODULE_TYPES, VALID_PIPETTES, VALID_SLOTS)
Linee 200-850   │ UNIVERSAL_TEMPLATE - Codice Python generato
Linee 860-1020  │ RecipeValidator - Validazione pre-generazione
Linee 1025-1120 │ ProtocolSimulator - Simulazione locale
Linee 1125-1380 │ ProtocolGenerator - Orchestrazione generazione
```

---

## Aggiungere un Nuovo Comando

### Passo 1: Aggiungere allo Schema di Validazione

Cerca `COMMAND_SCHEMA` (circa linea 103) e aggiungi la definizione:

```python
COMMAND_SCHEMA = {
    # ... comandi esistenti ...
    
    # Nuovo comando
    "NuovoComando": {
        "required": ["CampoObbligatorio1", "CampoObbligatorio2"],
        "optional": ["CampoOpzionale1", "CampoOpzionale2"],
        # Se il comando usa posizioni, aggiungi:
        "location_fields": ["Location", "Labware"]  # almeno uno richiesto
    },
}
```

**Regole per lo schema:**
- `required`: campi che DEVONO essere presenti nel JSON
- `optional`: campi che POSSONO essere presenti
- `location_fields`: se presente, almeno uno di questi campi deve esistere

### Passo 2: Aggiungere l'Handler nel UNIVERSAL_TEMPLATE

Cerca la sezione appropriata nel template e aggiungi l'handler:

```python
# Nel UNIVERSAL_TEMPLATE, trova la sezione corretta e aggiungi:

elif cmd == "NuovoComando":
    # Estrai parametri dal JSON
    campo1 = step.get("CampoObbligatorio1")
    campo2 = step.get("CampoObbligatorio2")
    opzionale = step.get("CampoOpzionale1", "valore_default")
    
    # Esegui l'operazione usando l'API Opentrons
    # Esempio: ctx.metodo_opentrons(campo1, campo2)
    ctx.comment(f"Eseguito NuovoComando con {campo1}")
```

### Passo 3: Testare

```bash
cd SiLA2/OpentronsSiLA2Server/src
python test_validation.py
```

### Esempio Completo: Aggiungere `CustomMix`

```python
# 1. In COMMAND_SCHEMA (dopo "Mix"):
"CustomMix": {
    "required": ["PipetteMount", "Volume", "Cycles"],
    "optional": ["Location", "Labware", "Rate", "BlowOutAfter"],
    "location_fields": ["Location", "Labware"]
},

# 2. Nel UNIVERSAL_TEMPLATE (sezione Liquid Handling):
elif cmd == "CustomMix":
    pip = objects["Pipettes"][step["PipetteMount"]]
    vol = step["Volume"]
    cycles = step["Cycles"]
    loc = get_loc(step.get("Location") or step.get("Labware"))
    rate = step.get("Rate", 1.0)
    
    for _ in range(cycles):
        pip.aspirate(vol, loc, rate=rate)
        pip.dispense(vol, loc, rate=rate)
    
    if step.get("BlowOutAfter"):
        pip.blow_out()
```

---

## Aggiornare per Nuova Versione API

### Quando Aggiornare

| Situazione | Azione |
|------------|--------|
| Opentrons rilascia bug fix | NON aggiornare (retrocompatibile) |
| Nuova funzionalità necessaria | Valutare se davvero serve |
| Metodo deprecato con warning | Pianificare aggiornamento |
| Metodo rimosso | Aggiornamento OBBLIGATORIO |

### Come Aggiornare l'API Level

1. **Verifica compatibilità** nelle [release notes Opentrons](https://docs.opentrons.com/v2/versioning.html)

2. **Modifica l'apiLevel** nel UNIVERSAL_TEMPLATE:

```python
# Cerca questa riga nel UNIVERSAL_TEMPLATE:
metadata = {
    "protocolName": "__PROTOCOL_NAME__",
    "apiLevel": "2.27"  # ← Cambia qui
}
```

3. **Testa TUTTI i comandi** esistenti:
   - Alcuni metodi potrebbero avere nuovi parametri
   - Alcuni comportamenti potrebbero cambiare
   - Verifica con `opentrons_simulate`

### Tabella Versioni API

| Versione API | Supporto Minimo | Note |
|--------------|-----------------|------|
| 2.20 | Flex base | Supporto iniziale Flex |
| 2.21 | Absorbance reader | Nuovo modulo |
| 2.22 | Liquid classes | Gestione liquidi |
| 2.23 | Flex Stacker | Nuovo modulo |
| 2.27 | Concurrent modules | Operazioni parallele |

---

## Aggiungere Nuovo Hardware

### Nuovo Modulo

1. **Aggiungi a VALID_MODULE_TYPES**:

```python
VALID_MODULE_TYPES = [
    "heaterShakerModuleV1",
    "temperatureModuleV1", "temperatureModuleV2",
    # ... esistenti ...
    "nuovoModuloV1",  # ← Aggiungi qui
]
```

2. **Aggiungi comandi** per il modulo in COMMAND_SCHEMA e UNIVERSAL_TEMPLATE

### Nuova Pipetta

```python
VALID_PIPETTES = [
    "flex_1channel_50", "flex_1channel_1000",
    "flex_8channel_50", "flex_8channel_1000",
    "flex_96channel_1000",
    "flex_nuova_pipetta",  # ← Aggiungi qui
]
```

### Nuovi Slot (improbabile)

```python
VALID_SLOTS = ["A1", "A2", "A3", "B1", "B2", "B3", "C1", "C2", "C3", "D1", "D2", "D3"]
# Flex ha già tutti gli slot, ma per futuri robot:
# VALID_SLOTS.extend(["E1", "E2", "E3"])
```

---

## Troubleshooting

### Errore: "Unknown command: XYZ"

**Causa**: Il comando non è in COMMAND_SCHEMA
**Soluzione**: Aggiungi il comando allo schema

### Errore: "Required field 'X' missing"

**Causa**: Lo schema richiede un campo che il JSON non ha
**Soluzione**: 
- Verifica che il JSON abbia il campo
- Oppure sposta il campo da `required` a `optional` nello schema

### Errore durante simulazione

**Causa**: Il codice Python generato ha errori
**Soluzione**:
1. Guarda l'output della simulazione
2. Trova il numero di riga dell'errore
3. Correggi l'handler nel UNIVERSAL_TEMPLATE

### Il robot non esegue come previsto

**Debug**:
1. Genera il protocollo senza caricarlo:
   ```python
   from protocol_generator import ProtocolGenerator
   gen = ProtocolGenerator(enable_simulation=False)
   code = gen.generate_content(json_recipe)
   print(code)  # Esamina il codice generato
   ```

2. Simula localmente:
   ```bash
   python -m opentrons.simulate generated_protocol.py
   ```

---

## Testing

### Test Automatici

```bash
cd SiLA2/OpentronsSiLA2Server/src
python test_validation.py
```

### Test Manuale di un Comando

```python
import json
from protocol_generator import ProtocolGenerator

recipe = {
    "ProtocolName": "Test Nuovo Comando",
    "Pipettes": {"left": "flex_1channel_1000"},
    "Labware": {
        "tips": {"LoadName": "opentrons_flex_96_tiprack_1000ul", "Slot": "A1"},
        "plate": {"LoadName": "corning_96_wellplate_360ul_flat", "Slot": "B1"}
    },
    "TipRacks": {"left": ["tips"]},
    "Steps": [
        {"Command": "NuovoComando", "Param1": "value1", ...}
    ]
}

gen = ProtocolGenerator(enable_simulation=True)
result = gen.generate_and_validate(json.dumps(recipe))

print(f"Valido: {result.success}")
print(f"Errori: {result.errors}")
print(f"Warning: {result.warnings}")
if result.simulation:
    print(f"Simulazione: {result.simulation.success}")
```

### Checklist Pre-Rilascio

```
□ Tutti i test passano (test_validation.py)
□ Simulazione locale OK (opentrons_simulate)
□ Testato su robot reale (se possibile)
□ COMMAND_SCHEMA aggiornato
□ UNIVERSAL_TEMPLATE aggiornato
□ Documentazione aggiornata (se necessario)
□ Backup del file precedente
```

---

## Riferimenti API Opentrons

### Documentazione Ufficiale

- **Python API**: https://docs.opentrons.com/v2/
- **Versioning**: https://docs.opentrons.com/v2/versioning.html
- **Moduli**: https://docs.opentrons.com/v2/modules.html
- **Pipette API**: https://docs.opentrons.com/v2/pipettes.html
- **Release Notes**: https://github.com/Opentrons/opentrons/releases

### Comandi Più Usati (Riferimento Rapido)

| Comando | Uso | API Opentrons |
|---------|-----|---------------|
| `Aspirate` | Aspirare liquido | `pipette.aspirate(vol, loc)` |
| `Dispense` | Dispensare liquido | `pipette.dispense(vol, loc)` |
| `Transfer` | Trasferimento completo | `pipette.transfer(vol, src, dst)` |
| `PickUpTip` | Prendere punta | `pipette.pick_up_tip()` |
| `DropTip` | Rilasciare punta | `pipette.drop_tip()` |
| `Mix` | Miscelare | `pipette.mix(reps, vol, loc)` |
| `MoveLabware` | Spostare labware | `protocol.move_labware(lw, dest)` |
| `HeaterShaker` | Controllo HS | `hs_module.set_target_temperature()` |
| `Thermocycler` | Controllo TC | `tc_module.set_block_temperature()` |

### Oggetti Disponibili nel Template

Nel UNIVERSAL_TEMPLATE, questi oggetti sono disponibili:

```python
ctx          # ProtocolContext - contesto principale
objects      # Dict con:
  ├── "Labware"   # {id: labware_object}
  ├── "Modules"   # {id: module_object}
  ├── "Pipettes"  # {"left": pipette, "right": pipette}
  ├── "Liquids"   # {id: liquid_object}
  └── "Adapters"  # {id: adapter_object}
get_loc(s)   # Funzione helper: "plate:A1" → labware["A1"]
settings     # Dict originale del JSON recipe
```

---

## Backup e Rollback

### Prima di Modificare

```bash
# Crea backup
cp protocol_generator.py protocol_generator.py.backup_YYYYMMDD
```

### Se Qualcosa Va Storto

```bash
# Ripristina
cp protocol_generator.py.backup_YYYYMMDD protocol_generator.py
```

### Versioning

Il file `protocol_generator.py` dovrebbe essere sotto controllo versione (git). In caso di problemi:

```bash
git diff protocol_generator.py  # Vedi modifiche
git checkout protocol_generator.py  # Annulla modifiche
git log protocol_generator.py  # Storico modifiche
```

---

## Contatti e Supporto

- **Opentrons Support**: support@opentrons.com
- **Forum Community**: https://community.opentrons.com/
- **GitHub Issues**: https://github.com/Opentrons/opentrons/issues

---

*Ultimo aggiornamento: Gennaio 2026*
*API Level corrente: 2.27*
