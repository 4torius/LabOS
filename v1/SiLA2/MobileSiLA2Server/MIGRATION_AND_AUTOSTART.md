# MobileSiLA2Server — Migrazione su un altro PC e Avvio Automatico

> **Nota (deployment corrente)**: il deployment di produzione oggi (host
> `192.168.11.22`, utente `zotac`) usa **Docker Compose** (container
> `mobile-sila2`, `restart: always`), non systemd — vedi `DEPLOYMENT.md`
> per la procedura corrente. Questa guida documenta il percorso di
> migrazione/avvio automatico **nativo/systemd** usato sul PC precedente
> (`10.16.0.61`), tenuta per riferimento nel caso serva di nuovo un
> deployment bare-metal senza Docker.

Guida verificata sul campo il 2026-07-30, eseguendo l'intero processo (setup accesso SSH, verifica ROS, installazione del servizio systemd) sul PC
`10.16.0.61` (`iitreh002lw012u`, utente `inspection`).

## Panoramica

Il server SiLA2 per il manipolatore mobile (GoFaGo) **deve girare sul PC
Linux** collegato fisicamente al robot, perché comunica con ROS via `rospy`.
L'orchestratore LabOS (sul PC Windows) si collega ad esso via gRPC sulla
porta **50053**. Spostare il server su un altro PC significa: copiare la
cartella, verificare ROS/rpwc_msgs, aggiornare l'IP lato orchestratore, e
(opzionale ma consigliato) registrarlo come servizio systemd per l'avvio
automatico.

## Prerequisiti sul nuovo PC

- Ubuntu con ROS Noetic (o Melodic) installato in `/opt/ros/`
- Un catkin workspace con `rpwc_msgs` compilato (i messaggi ROS custom usati
  dal bridge — action `executionCommandsAction`, service `getSubtaskInfo`, ecc.)
- Python 3 con `pip3`
- Un utente con accesso SSH e sudo

## Passo 1 — Copiare la cartella del server

Dal PC attuale (o da un backup), copia l'intera cartella `MobileSiLA2Server`
sul nuovo PC. `rsync` è preferibile perché preserva permessi/timestamp ed
esclude i file di cache:

```bash
rsync -avz --exclude '__pycache__' --exclude '*.pyc' \
  inspection@VECCHIO_IP:/home/inspection/SiLA2/MobileSiLA2Server/ \
  utente@NUOVO_IP:/home/utente/SiLA2/MobileSiLA2Server/
```

In alternativa, da un PC Windows con la cartella in locale:

```powershell
scp -r MobileSiLA2Server/ utente@NUOVO_IP:/home/utente/SiLA2/
```

## Passo 2 — Verificare/preparare ROS sul nuovo PC

```bash
ssh utente@NUOVO_IP

# ROS installato?
ls /opt/ros/

# Dove si trova rpwc_msgs?
find ~ -iname "rpwc_msgs" -type d 2>/dev/null

# Se non è compilato:
cd ~/percorso/al/catkin_ws
catkin_make
source devel/setup.bash
python3 -c "from rpwc_msgs.msg import executionCommandsAction; print('OK')"
```

**Nota**: il server funziona anche senza ROS master attivo — riprova la
connessione ogni 15s in background senza crashare — ma le funzionalità di
navigazione/esecuzione task restano indisponibili finché ROS non risponde.

## Passo 3 — Installare dipendenze con lo script già pronto

```bash
cd /home/utente/SiLA2/MobileSiLA2Server
chmod +x install.sh
./install.sh
```

Lo script `install.sh`:
1. rileva ROS (Noetic/Melodic)
2. installa le dipendenze Python (`requirements.txt`)
3. verifica che `sila2` e gli stub generati (`generated/`) siano presenti
4. verifica `rpwc_msgs` nel workspace sorgentato
5. crea la cartella `logs/`
6. prova a installare il servizio systemd (**fallisce silenziosamente senza
   sudo passwordless** — in quel caso segui il Passo 5 manualmente)

## Passo 4 — Aggiornare la configurazione sull'orchestratore (Windows)

In `v1/lab_config.yaml`, sezione `servers.mobile`:

```yaml
servers:
  mobile:
    host: NUOVO_IP        # IP del nuovo PC — verificalo con `hostname -I` sul PC Linux
    port: 50053
    network:
      ros_master_uri: http://localhost:11311   # se il ROS master gira sullo stesso PC
      ros_namespace: /setup1                    # adatta se il namespace cambia
```

Poi **riavvia la webapp** (il processo Python di `webapp/app.py`, o tramite
`launcher.py`) perché ricarichi `lab_config.yaml` — non lo rilegge a caldo.

## Passo 5 — Avvio automatico al boot (systemd)

Il repo include già un template di servizio (`mobile_sila2.service`) e uno
script wrapper (`start_service.sh`) che sorgono ROS/catkin correttamente —
systemd non esegue `.bashrc`, quindi l'ambiente va sorgentato esplicitamente
(già gestito dallo script, non serve modificarlo).

### 5a — Genera il file di servizio con i placeholder compilati

```bash
cd /home/utente/SiLA2/MobileSiLA2Server
sed "s|__INSTALL_DIR__|$(pwd)|g; s|__SERVICE_USER__|$USER|g" \
  mobile_sila2.service > /tmp/mobile_sila2_resolved.service
```

### 5b — Installa e abilita il servizio (richiede sudo)

```bash
sudo cp /tmp/mobile_sila2_resolved.service /etc/systemd/system/mobile_sila2.service
sudo systemctl daemon-reload
sudo systemctl enable mobile_sila2
```

`enable` basta da solo per far partire il servizio automaticamente a ogni
riavvio da questo momento in poi — non serve avviarlo subito se preferisci
lasciare girare un'istanza manuale fino al prossimo reboot.

### 5c — Avvia subito (opzionale)

Se c'è già un processo avviato manualmente, fermalo prima (altrimenti la
porta 50053 resta occupata e systemd non riesce a far partire il suo):

```bash
ss -tlnp | grep 50053      # trova il PID in ascolto
kill PID_TROVATO
```

Poi avvia via systemd:

```bash
sudo systemctl start mobile_sila2
```

## Passo 6 — Verifica

```bash
# Stato del servizio — deve mostrare "enabled" e "active (running)"
systemctl status mobile_sila2 --no-pager
systemctl is-enabled mobile_sila2
systemctl is-active mobile_sila2

# Porta in ascolto
ss -tlnp | grep 50053

# Log in tempo reale
sudo journalctl -u mobile_sila2 -f
```

Dal lato Windows, verifica che l'orchestratore lo veda online chiamando:

```
GET http://localhost:5000/api/devices
```

L'entry `"id": "mobile"` deve avere `"status": "online"` con il nuovo IP.

## Comandi utili di gestione

```bash
sudo systemctl stop mobile_sila2       # ferma il servizio
sudo systemctl restart mobile_sila2    # riavvia (es. dopo un aggiornamento del codice)
sudo systemctl disable mobile_sila2    # disabilita l'avvio automatico
sudo journalctl -u mobile_sila2 -n 100 --no-pager   # ultime 100 righe di log
```

## Troubleshooting

**Il servizio non parte / "Unit mobile_sila2.service could not be found"**
Verifica che il file sia stato copiato in `/etc/systemd/system/` con il nome
esatto `mobile_sila2.service` e che `sudo systemctl daemon-reload` sia stato
eseguito dopo la copia.

**Il servizio parte ma va subito in errore**
Controlla i log: `sudo journalctl -u mobile_sila2 -n 50 --no-pager`. Cause
comuni:
- ROS non trovato (`/opt/ros/noetic/setup.bash` mancante) — verifica il
  percorso in `start_service.sh`
- `rpwc_msgs` non trovato nel workspace sorgentato — `start_service.sh`
  cerca in ordine `~/exsensia/tmp_ws`, `~/exsensia/catkin_ws`, `~/exsensia`,
  `~/catkin_ws`: aggiungi il tuo percorso se diverso
- Porta 50053 già occupata da un processo manuale precedente — vedi Passo 5c

**ROS master non raggiungibile**
Non è un errore fatale: il server logga `ROS master not reachable — will
retry later` e riprova ogni 15s. Verifica che `roscore` sia in esecuzione se
ti aspetti che le funzionalità di navigazione/esecuzione task rispondano.

**L'orchestratore Windows non lo vede online**
- Verifica `lab_config.yaml` → `servers.mobile.host` punti all'IP corretto
  (e che la webapp sia stata riavviata dopo la modifica)
- Verifica che la porta 50053 sia raggiungibile dalla rete Windows:
  `Test-NetConnection NUOVO_IP -Port 50053` (PowerShell)
- Controlla eventuale firewall sul PC Linux: `sudo ufw status` /
  `sudo ufw allow 50053/tcp`

## Accesso SSH per gestione remota (opzionale)

Per gestire il server da remoto (incluso da un assistente come questo) senza
password ogni volta, si può registrare una chiave pubblica dedicata in
`~/.ssh/authorized_keys` sull'utente del PC Linux:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "CHIAVE_PUBBLICA_QUI" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

La chiave privata corrispondente non deve mai lasciare la macchina da cui ci
si connette, e va usata una chiave dedicata (non quella personale) così può
essere revocata singolarmente rimuovendo solo quella riga da
`authorized_keys`.
