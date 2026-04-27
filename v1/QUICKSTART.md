# BicoccaLab v7 - Quickstart Guide

Guida rapida per installare e avviare il sistema di automazione laboratorio.

---

## Requisiti

- **Python 3.10+** (consigliato 3.12 o 3.13)
- **Windows 10/11** (per lo script di setup)
- **Connessione di rete** al robot Opentrons Flex (per funzionalità complete)

---

## 1. Setup Ambiente (Prima volta)

Aprire PowerShell nella cartella `v7` ed eseguire:

```powershell
.\setup_env.ps1
```

Questo script:
- Crea un virtual environment (`.venv`)
- Installa tutte le dipendenze (gRPC, FastAPI, zeroconf, ecc.)
- Verifica l'installazione

**Opzioni avanzate:**
```powershell
.\setup_env.ps1 -Force      # Ricrea l'ambiente da zero
.\setup_env.ps1 -Verbose    # Mostra output dettagliato
.\setup_env.ps1 -SkipVenv   # Solo aggiorna dipendenze
```

---

## 2. Attivare l'Ambiente

Dopo il setup, attivare il virtual environment:

```powershell
.\.venv\Scripts\Activate.ps1
```

Il prompt mostrerà `(.venv)` quando l'ambiente è attivo.

---

## 3. Avviare il Sistema

### Metodo rapido (tutto insieme):
```powershell
.\START.ps1
```

### Avvio manuale dei componenti:

**1. Web Application (UI principale):**
```powershell
python start_webapp.py
```
Accedere a: http://127.0.0.1:5000

**2. Server Opentrons (se collegato al robot):**
```powershell
python -m SiLA2.OpentronsSiLA2Server
```

**3. Orchestrator (gestione workflow):**
```powershell
python scripts/start_orchestrator.py
```

---

## 4. Configurazione

### File principale: `lab_config.yaml`

```yaml
# Indirizzo IP del robot Opentrons Flex
opentrons:
  ip: "192.168.1.100"    # Modificare con IP reale
  port: 31950

# Porte SiLA2
sila2:
  opentrons_port: 50052
  tecan_port: 50051
  manual_port: 50360
```

### Hardware Abstraction Layer (HAL)

I file di configurazione hardware sono in:
```
Library/HardwareConfig/
├── default.yaml          # Configurazione standard
├── maintenance.yaml      # Per manutenzione
└── custom.yaml           # Personalizzata
```

---

## 5. Struttura Progetto

```
v7/
├── setup_env.ps1         # Script setup ambiente
├── START.ps1             # Avvia tutto il sistema
├── start_webapp.py       # Avvia webapp standalone
├── lab_config.yaml       # Configurazione principale
├── requirements.txt      # Dipendenze Python
│
├── webapp/               # Interfaccia web (FastAPI)
├── SiLA2/                # Server SiLA2
│   ├── OpentronsSiLA2Server/
│   ├── TecanSiLA2Server/
│   └── ManualStationSiLA2Server/
│
├── Library/              # Configurazioni e ricette
│   ├── HardwareConfig/   # Configurazioni HAL
│   ├── Recipes/          # Ricette JSON
│   └── Workflows/        # Workflow YAML
│
├── Results/              # Output generati
└── docs/                 # Documentazione
```

---

## 6. Comandi Utili

### Test connessione robot:
```powershell
python -c "import httpx; r = httpx.get('http://192.168.1.100:31950/health'); print(r.json())"
```

### Rigenerare stub gRPC:
```powershell
python regen_stubs.py
```

### Test sistema:
```powershell
python test_commands.py
```

---

## 7. Troubleshooting

### "Module not found" errors
```powershell
# Ricreare l'ambiente
.\setup_env.ps1 -Force
```

### Robot non raggiungibile
1. Verificare IP in `lab_config.yaml`
2. Ping al robot: `ping 192.168.1.100`
3. Controllare firewall

### mDNS "not available"
```powershell
# Reinstallare zeroconf
.\.venv\Scripts\pip.exe install --force-reinstall zeroconf
```

### Ambiente corrotto
```powershell
# Eliminare e ricreare
Remove-Item .venv -Recurse -Force
.\setup_env.ps1
```

---

## 8. Portare su un altro PC

1. **Copiare/clonare** la cartella `v7` (o usare git)
2. **Eseguire setup:**
   ```powershell
   cd path\to\v7
   .\setup_env.ps1
   ```
3. **Modificare** `lab_config.yaml` con gli IP corretti
4. **Avviare:** `.\START.ps1`

Il file `.gitignore` esclude automaticamente:
- `.venv/` (ambiente virtuale - viene ricreato)
- `__pycache__/` (cache Python)
- `Results/` (dati generati)
- `*.local.yaml` (config specifiche della macchina)

---

## Prossimi Passi

- 📖 [Architettura del sistema](docs/ARCHITECTURE.md)
- 🔧 [Aggiungere nuovi strumenti](docs/ADDING_NEW_INSTRUMENT.md)
- 📋 [Creare workflow](docs/PROTOCOLS.md)
- 🤖 [Configurazione Opentrons](docs/OPENTRONS_SERVER.md)

---

**Versione:** v7  
**Ultimo aggiornamento:** Febbraio 2026
