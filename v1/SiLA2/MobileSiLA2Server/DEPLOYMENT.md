# MobileSiLA2Server - Deployment Guide

> Aggiornato per riflettere il deployment attualmente in produzione (Docker,
> host `192.168.11.22`, utente `zotac`). La versione precedente di questo
> file descriveva un deployment più vecchio, nativo/systemd, su un PC diverso
> (`10.16.0.114`/`10.16.0.61`, utente `inspection`) — quella procedura è
> ancora documentata in `MIGRATION_AND_AUTOSTART.md` per riferimento, nel
> caso servisse di nuovo un deployment bare-metal, ma **non è il deployment
> corrente**.

## Architettura attuale

```
┌─────────────────────────────────────────────────────────────────────────┐
│                       PC ORCHESTRATOR (Windows)                          │
│    LabOS (v1/): LabCore + PnP Discovery + WebApp                         │
└─────────────────────────────┬─────────────────────────────────────────────┘
              │ SiLA2 gRPC, porta 50053 (LAN)
              ▼
┌─────────────────────────────────────────────────────────────────────────┐
│              HOST LINUX (zbox) — 192.168.11.22, utente zotac             │
│                                                                          │
│  Docker Compose stack (docker ps mostra tutti "Up", restart: always):    │
│    - mobile-sila2       ← QUESTO server (SiLA2 + rospy)                 │
│    - ros-rpwc           ← ROS + rpwc_msgs (immagine exsensia:bicocca_release)
│    - webappexsensia-*   ← app/nginx/mongo di Exsensia (LfD, teaching)    │
│                                                                          │
│  mobile-sila2 (questo server):                                          │
│    - Basato su sila2 library (stesso pattern dei server locali LabOS)   │
│    - Feature: TaskManagement (vedi features/TaskManagement.sila.xml)     │
│    - LabOSLibraryProvider: dichiara "mobile_task" → Library/MobileTasks/│
│    - mDNS discovery abilitata (--no-discovery per disabilitarla)        │
│    - Loop di riconnessione ROS in background: se roscore muore/riparte, │
│      il server si auto-termina (SIGTERM) e Docker (restart: always) lo  │
│      rialza con una sessione rospy pulita                                │
│                                                                          │
│  Comunica con ROS (dentro ros-rpwc) via:                                 │
│    - Service  /setup1/getSubtasksInfo   → discovery task (RefreshTasks) │
│    - Action   /setup1/state_exec        → esecuzione task (ExecuteTask) │
│    - Service  /setup1/set_hardware_activation → (De)ActivateAllHardware,│
│                                             SetHardwareActivation(ByRole)│
│    - Service  getSetupInfo              → risolve HardwareId per ruolo  │
│                                                                          │
└─────────────────────────────────────────────────────────────────────────┘
```

**Il server SiLA2 gira dentro un container Docker sull'host Linux** perché
deve comunicare con ROS tramite `rospy`, e riusa l'immagine base
`exsensia:bicocca_release` per garantire compatibilità con `rpwc_msgs`.

## Comandi SiLA2 reali (da `features/TaskManagement.sila.xml`)

Proprietà: `AvailableTasks`, `AvailableHardware`, `CurrentTaskId`,
`CurrentSubtask`, `TaskStatus`, `RobotConnectionStatus`.

Comandi: `RefreshTasks`, `ExecuteTask(TaskId, ExecutionMode)`, `CancelTask`,
`GetTaskDetails(TaskId)`, `ConnectToRobot(RosMasterUri)`,
`SetHardwareActivation(HardwareId, Activate)`,
`SetHardwareActivationByRole(Hardware, Activate)`, `ActivateAllHardware`,
`DeactivateAllHardware`.

`ExecuteTask` e i comandi di attivazione hardware sono azioni fisicamente
rilevanti (muovono l'arm o attivano motori/servo) — verificare sempre
presenza/supervisione fisica prima di chiamarli, specialmente
`ActivateAllHardware`.

## Verifica stato del deployment

```bash
# Sull'host Linux (o via SSH da remoto)
docker ps                          # mobile-sila2 deve essere "Up"
docker logs mobile-sila2 --tail 50 # ultimi log

# Verifica dal lato Windows che l'orchestratore lo veda online
# GET http://localhost:5000/api/devices → l'entry "mobile" deve avere status "online"
```

## Riavviare il server dopo una modifica al codice

```bash
ssh zotac@192.168.11.22
cd ~/MobileSiLA2Server
docker compose restart mobile-sila2
# oppure, se serve rebuildare l'immagine dopo un cambio a Dockerfile/requirements:
docker compose up -d --build mobile-sila2
```

## Modificare la configurazione lato orchestratore (Windows)

In `v1/lab_config.yaml`, sezione `servers.mobile`:

```yaml
servers:
  mobile:
    host: 192.168.11.22
    port: 50053
    remote: true
    network:
      ros_master_uri: http://localhost:11311
      ros_namespace: /setup1
```

Riavvia la webapp (`launcher.py` o `webapp/app.py`) dopo qualunque modifica —
`lab_config.yaml` non viene riletto a caldo.

## Troubleshooting

**Il container non parte o va in crash loop**
```bash
docker logs mobile-sila2 --tail 100
docker ps -a   # controlla lo stato/exit code
```

**`RobotConnectionStatus` resta su `Disconnected`/`Connecting`**
Non è necessariamente un errore fatale: il loop di riconnessione interno
riprova periodicamente senza andare in crash. Verifica che `ros-rpwc` sia
`Up` e che `roscore` risponda dentro quel container prima di sospettare un
bug nel server SiLA2.

**L'orchestratore Windows non lo vede online**
- Verifica `lab_config.yaml` → `servers.mobile.host` = `192.168.11.22` (e
  che la webapp sia stata riavviata dopo qualunque modifica)
- Verifica che la porta 50053 sia raggiungibile dalla rete Windows:
  `Test-NetConnection 192.168.11.22 -Port 50053` (PowerShell)

## Accesso SSH per gestione remota

Chiave dedicata registrata in `~/.ssh/authorized_keys` sull'host
(`zotac@192.168.11.22`), separata dalle chiavi personali così può essere
revocata singolarmente. La chiave privata corrispondente vive solo sul PC
orchestratore Windows da cui ci si connette.

```bash
ssh -i ~/.ssh/id_ed25519_labos_mobile zotac@192.168.11.22
```

---

*Le sezioni originali su ROS message definitions (`executionCommands.action`,
`getSubtaskInfo.srv`, `infoTasks.msg`) restano valide e non sono ripetute
qui — vedi la history di questo file o `features/TaskManagement.sila.xml`
per i dettagli dei messaggi ROS sottostanti.*
