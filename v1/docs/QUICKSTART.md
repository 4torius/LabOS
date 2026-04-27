# 🚀 Guida Rapida - BicoccaLab

Guida veloce per iniziare a usare il sistema BicoccaLab.

---

## ⚡ Avvio in 3 passi

### 1. Avvia l'API

Doppio click su **`StartAPI.bat`**

Aspetta di vedere:
```
BicoccaLab Orchestrator API Ready
REST API: http://localhost:8000
```

### 2. Apri l'interfaccia Web

Vai a: **http://localhost:8000/ui**

### 3. Avvia i server dalla Web UI

Nella barra superiore trovi i **toggle dei server**:

| Toggle | Funzione |
|--------|----------|
| **Tecan Server** | Avvia/ferma il server SiLA2 Tecan |
| **Opentrons Server** | Avvia/ferma il server SiLA2 Opentrons |
| **Node-RED** | Avvia/ferma Node-RED per protocolli |

Clicca su ogni toggle per avviare i server necessari. I LED di stato nella barra indicano:
- 🟢 Verde = Connesso
- 🔴 Rosso = Disconnesso

---

## 🎮 Usare la Web Dashboard

### Dashboard principale

La pagina Dashboard mostra:
- Stato in tempo reale di tutti gli strumenti
- Card per Tecan con controlli rapidi
- Card per Opentrons con controlli rapidi
- Card Orchestrator per eseguire workflow

### Controlli rapidi Tecan

| Controllo | Funzione |
|-----------|----------|
| **Seleziona metodo + Esegui** | Avvia analisi |
| **Temperatura** | Imposta/spegni controllo temperatura |
| **Piatto Dentro/Fuori** | Movimento cassetto |
| **Connetti/Disconnetti** | Connessione a iControl |

### Controlli rapidi Opentrons

| Controllo | Funzione |
|-----------|----------|
| **Seleziona recipe + Esegui** | Avvia protocollo |
| **Pausa/Riprendi/Annulla** | Controllo run |
| **Home / Luci** | Movimenti base |
| **Reset Punte** | Reset tip tracking |

### Emergency Stop

⚠️ Il pulsante **EMERGENCY STOP** rosso nella barra superiore ferma immediatamente tutti gli strumenti!

---

## 🔧 Protocol Designer (Node-RED)

Vai a: **Protocols Designer** dal menu laterale

O apri direttamente: **http://localhost:1880**

### Pannello principale

Trovi il flow **"BicoccaLab Orchestrator"** con questi bottoni:

| Bottone | Funzione |
|---------|----------|
| 🔵 **Get Status** | Mostra lo stato degli strumenti |
| 📋 **List Workflows** | Elenca i workflow disponibili |
| 🚨 **EMERGENCY STOP** | Ferma tutto immediatamente |

### Eseguire un workflow

1. Trova la sezione "WORKFLOW RUNNER"
2. Clicca su uno dei bottoni:
   - **Run: Transfer_And_Read** - Trasferisce e legge
   - **Run: ELISA Example** - Workflow ELISA completo
   - **Run: Simple Plate Read** - Solo lettura Tecan

3. Guarda il risultato nel pannello **Debug** (a destra)

### Controllo diretto strumenti

Sezione "DIRECT INSTRUMENT CONTROL":

- **Opentrons: Home** - Riporta il robot alla posizione iniziale
- **Opentrons: modifiedtest1.json** - Esegue una recipe specifica
- **Tecan: TestFluo.mdfx** - Esegue analisi fluorescenza

---

## 📊 Vedere i risultati

### Da Node-RED

Clicca **"List Results"** per vedere i file recenti.

### Da file system

I risultati sono in `BicoccaLab/Results/`:

```
Results/
├── CSV/          ← Dati tabulari
├── XML/          ← Dati strutturati
├── Excel/        ← Report
└── AnIML/        ← Standard scientifico
```

---

## 🛠️ Creare un nuovo workflow

### 1. Copia un template

```
BicoccaLab/Protocols/Simple_PlateRead.workflow.json
```

### 2. Modifica con un editor di testo

```json
{
  "workflow": {
    "id": "mio_workflow",
    "name": "Il Mio Workflow",
    "description": "Cosa fa"
  },
  "steps": [
    {
      "name": "Step 1",
      "instrument": "tecan",
      "action": "run_analysis",
      "params": {
        "method_file": "TestFluo.mdfx"
      }
    }
  ]
}
```

### 3. Salva in `Protocols/`

Il workflow apparirà automaticamente nella lista.

---

## ⚠️ In caso di problemi

### L'API non risponde

```batch
:: Verifica che la porta sia libera
netstat -an | findstr 8000

:: Riavvia l'API
StartAPI.bat
```

### Node-RED non si apre

```batch
:: Verifica la porta
netstat -an | findstr 1880

:: Riavvia Node-RED
StartNodeRED.bat
```

### Strumento disconnesso

1. Verifica che lo strumento sia acceso
2. Clicca "Get Status" in Node-RED
3. Se mostra "disconnected", riavvia il server dello strumento

### Emergency Stop

Se qualcosa va storto:

1. Clicca **🚨 EMERGENCY STOP** in Node-RED
2. Oppure vai a: `http://localhost:8000/emergency-stop` (POST)
3. In caso estremo, usa il pulsante fisico sullo strumento

---

## 📞 Supporto

- **API Docs interattiva**: http://localhost:8000/docs
- **Documentazione completa**: `README.md`
- **Documentazione tecnica**: `docs/TECHNICAL.md`

---

*BicoccaLab - Laboratorio Robotizzato*
