# Report Operativo Progetto LabOS 2.0 (BicoccaLab v7)

Data: 20/04/2026  
Ambito: stato avanzamento tecnico-operativo basato su tesi + codice implementato nel repository

## 1) Executive Summary

Il progetto ha completato la parte core della piattaforma software per automazione di laboratorio plug-and-play (discovery, orchestrazione, interfacce operatore, integrazione SiLA2 multi-strumento).

Lo stato attuale e' da considerarsi **pre-produzione avanzata**:
- Architettura modulare implementata e funzionante.
- Integrazione software di Opentrons, Tecan e Manual Station disponibile.
- Mobile robot integrato a livello architetturale/protocollare, con validazione end-to-end ancora dipendente dalla piena disponibilita' hardware in linea (coerente con tesi).
- Framework di workflow, intervento umano e tracciabilita' gia' presenti nella webapp.

## 2) Stato Implementazione per Area

### A. Core Orchestrazione (completato)
- `LabCore` come interfaccia unificata per CLI e WebApp.
- Cache discovery con TTL per ridurre roundtrip gRPC.
- Gestione comandi, file library (recipes/analisi/HAL/workflow), esecuzione workflow.

Evidenza codice:
- `src/lab_core.py`
- `src/pnp_workflow_executor.py`

### B. Discovery Plug-and-Play (completato)
Discovery multi-sorgente gia' implementata:
- mDNS/DNS-SD
- scansione porte
- `lab_config.yaml`
- scanning directory server SiLA2

Evidenza codice/documentazione:
- `src/pnp_discovery.py`
- `ARCHITECTURE.md`

### C. Integrazione Strumenti (completato lato software, parziale lato operativita' hardware)
Server configurati nel sistema:
- Tecan M200 Pro (`localhost:50051`)
- Opentrons Flex (`localhost:50057`)
- Manual Station (`localhost:50360`)
- Mobile Robot (`192.168.11.22:50053`, remoto)

Evidenza config:
- `lab_config.yaml`

### D. Interfacce Utente e Operations (completato)
- CLI Plug-and-Play dinamica (nessun menu hardcoded).
- WebApp FastAPI con dashboard, workflow, risultati, gestione interventi operatore.
- Endpoint API numerosi (rilevate 65 route tra HTML/API/WebSocket).

Evidenza:
- `pnp_console.py`
- `webapp/app.py`

### E. Workflow & Human-in-the-loop (completato)
- Esecuzione workflow con dipendenze e parallelismo.
- Categorizzazione errori (timeout, device unavailable, hardware error, etc.).
- Strategia retry + richiesta intervento operatore (retry/skip/abort).

Evidenza:
- `src/pnp_workflow_executor.py`
- `webapp/app.py`

### F. Tracciabilita' dati e risultati (completato in prima versione)
- Cartelle risultati strutturate (`CSV`, `Excel`, `AnIML`, `XML`, `opentrons`).
- Gestione plate tracking persistente (`Results/plate_tracking.json`).

Evidenza:
- `Results/`
- `webapp/app.py`

## 3) Evidenze di Validazione (eseguite oggi)

### 3.1 Test suite completa (stato attuale)
Comando eseguito:
- `python -m pytest -q`

Esito:
- Interrotta in fase collection con 5 errori (import path e dipendenze mancanti nell'environment corrente).

Errori principali rilevati:
- `ModuleNotFoundError: src.lab_core` / `src.pnp_discovery`
- `ModuleNotFoundError: requests`
- test locale Opentrons con import `protocol_generator` non risolto

Interpretazione:
- Non indica regressione funzionale del core, ma necessita allineamento ambiente di test (PYTHONPATH/deps/isolamento test locali server).

### 3.2 Test core piattaforma (rieseguiti con PYTHONPATH esplicito)
Comando:
- `pytest tests/test_xml_parser.py tests/test_discovery.py -q -ra`

Esito:
- **18 passed, 4 skipped**
- Skip motivati da server hardware non attivi al momento del test.

### 3.3 Test integrazione LabCore (subset)
Comando:
- `pytest tests/test_integration.py -q -ra`

Esito:
- **6 passed, 8 skipped**
- Skip per assenza server attivi (Opentrons/Tecan/Manual).

Conclusione tecnica validazione:
- Il core software e le componenti di discovery/parser risultano stabili.
- La validazione end-to-end hardware resta condizionata da disponibilita' contemporanea dei server strumento.

## 4) Allineamento con Tesi: Cosa e' stato consolidato

Dalla tesi risultano gia' centrali: SiLA2, modularita', orchestrazione DAG, HAL, human-in-the-loop, UI web.

Nel codice attuale questi elementi sono presenti e operativi:
- orchestrazione workflow + gestione errori/interventi;
- integrazione server eterogenei (Python/C#) via SiLA2;
- UI web operativa con API estese e WebSocket;
- struttura Library/Results pronta all'uso operativo.

Elemento ancora in fase di chiusura operativa completa:
- validazione end-to-end con mobile manipulator in linea e campaign completa multi-strumento.

## 5) Rischi Operativi Residui

1. **Rischio di readiness test ambiente**
- Suite completa non ancora "one-command green" su environment attuale.
- Impatto: medio (rallenta certificazione e handover).

2. **Dipendenza da disponibilita' hardware simultanea**
- Molti test integrazione sono skip-safe ma non eseguibili senza strumenti online.
- Impatto: medio-alto (evidenze E2E meno frequenti).

3. **Mobile robot: fase deployment**
- Integrazione software pronta, ma validazione operativa completa dipendente dal setup finale robot.
- Impatto: alto sulle demo di piena autonomia logistica.

## 6) Piano Operativo Proposto (prossimi 90 giorni)

### M1 (0-30 gg): Hardening tecnico
- Stabilizzare test environment (`requirements`, `PYTHONPATH`, separazione test locali server).
- Obiettivo: pipeline test ripetibile con report automatico pass/skip/fail.

### M2 (30-60 gg): Validazione integrata laboratorio
- Sessioni con Opentrons + Tecan + Manual Station in parallelo.
- Misure target: successo workflow, tempi medi, tasso intervento operatore.

### M3 (60-90 gg): Chiusura mobile E2E
- Collegamento operativo stabile con Mobile Robot.
- Esecuzione scenario completo (preparazione -> trasporto -> lettura -> tracciabilita').
- Deliverable: report KPI finale e readiness per fase pre-pilota.

## 7) KPI di Reporting Consigliati ai Finanziatori

- Disponibilita' server per strumento (% uptime sessione)
- Success rate comandi SiLA2 per strumento
- Success rate workflow end-to-end
- Tempo medio completamento workflow
- Numero medio interventi umani per workflow
- Copertura test: passed / skipped / failed

## 8) Richiesta Operativa ai Finanziatori (messaggio sintetico)

Il progetto ha superato la fase di prototipazione software ed e' in fase di consolidamento operativo multi-strumento. L'investimento nel prossimo trimestre massimizza il ritorno su quanto gia' sviluppato, trasformando una piattaforma tecnicamente matura in una soluzione validata end-to-end per uso continuativo in laboratorio.

## 9) Fonti interne utilizzate

- Tesi:
  - `thesis/chapters/05_integration.tex`
  - `thesis/chapters/07_experimental_validation.tex`
  - `thesis/chapters/08_conclusions.tex`
- Codice e architettura:
  - `src/lab_core.py`
  - `src/pnp_discovery.py`
  - `src/pnp_workflow_executor.py`
  - `webapp/app.py`
  - `pnp_console.py`
  - `lab_config.yaml`
- Test:
  - `tests/test_xml_parser.py`
  - `tests/test_discovery.py`
  - `tests/test_integration.py`
