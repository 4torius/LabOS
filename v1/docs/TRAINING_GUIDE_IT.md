# LabOS — Guida al Training (Italiano)

Traduzione italiana di [TRAINING_GUIDE.md](TRAINING_GUIDE.md), pensata per chi terrà la formazione in aula. In caso di dubbio o discrepanza, la versione inglese resta quella di riferimento (è quella aggiornata insieme al codice). Nomi di comandi, pagine, endpoint e percorsi file restano in inglese, come nel resto del codice/documentazione.

---

## 1. Il pitch in 30 secondi (apri la sessione così)

> "LabOS è il software che fa funzionare il nostro laboratorio automatizzato. Permette di descrivere un esperimento una sola volta — come ricetta e workflow — e far eseguire nell'ordine giusto, automaticamente, il robot, il lettore di piastre e gli step manuali, con controlli di sicurezza integrati. Si interagisce interamente tramite una pagina web; non serve mai scrivere codice per eseguire un esperimento."

Poi mostra lo schema architetturale di [ARCHITECTURE.md](ARCHITECTURE.md) se il pubblico è tecnico, oppure vai direttamente al browser se non lo è.

---

## 2. Cosa c'è davvero nel sistema oggi

Sii chiaro su questo punto fin da subito — evita confusione più avanti quando qualcuno chiede del robot mobile:

| Strumento | In uso attivo | Note |
|---|---|---|
| **Opentrons Flex** | Sì | Liquid handling, il server più completo (17 comandi, sicurezza tip, archivio run) |
| **Tecan Infinite M200 Pro** | Sì | Lettura piastre, tramite un bridge Windows in C# |
| **Manual Station** | Sì | Virtuale — nessun hardware, gestisce solo le richieste operatore nella web UI |
| **Robot mobile GoFaGo** | Sì | Manipolazione mobile + trasporto piastre, ospitato da remoto sul suo host Linux/Docker (`192.168.11.22`) — non fa parte di questo repository, ma viene scoperto e pilotato esattamente come gli strumenti locali |

---

## 3. Prima del training: checklist

Da eseguire la mattina stessa (o la sera prima) della sessione — niente rovina una demo più velocemente di un imprevisto tecnico.

- [ ] `cd v1 && python launcher.py --all` (oppure doppio click su `LabOS_launcher.bat`) parte senza errori in nessuna finestra di terminale
- [ ] http://localhost:5000 si carica, la Dashboard mostra Opentrons, Tecan, Manual Station e Mobile Robot tutti **online**
- [ ] Nella pagina **Hardware** è attiva una config HAL coerente con il deck fisico che hai allestito
- [ ] Il software iControl di Tecan è **chiuso** sulla macchina host (blocca la porta USB necessaria al bridge)
- [ ] Esegui una volta `Opentrons_Only_Test.workflow.json` per intero con una piastra di scarto — conferma che il liquid handling funzioni davvero oggi, non solo che il software risulti "online"
- [ ] Se il trasporto via robot mobile fa parte della demo: verifica che i marker ArUco su ogni postazione siano puliti e non ostruiti, e fai prima una `ExecuteTask` con `ExecutionMode: DryRun`

---

## 4. Agenda proposta (adatta ai tempi che hai a disposizione)

| Tempo | Argomento | Formato |
|---|---|---|
| 10 min | Il pitch + panoramica architetturale (§1, §2) | Discorso + schema |
| 15 min | Avvio del sistema, tour di tutte le pagine (§6) | Live nel browser |
| 20 min | Golden path: un esperimento reale dall'inizio alla fine (§7) | Demo live, fai guidare un partecipante |
| 10 min | Funzioni di sicurezza — cosa protegge, cosa no (§8) | Discorso + dimostrazione |
| 10 min | Cosa NON fare (§9) | Discorso — questa sezione previene la maggior parte delle richieste di supporto future |
| 10 min | Troubleshooting di base (§10) | Discorso + domande |
| 5 min | Dove trovare aiuto / log / questo documento | Chiusura |

---

## 5. Setup (se i partecipanti devono avere un proprio ambiente)

Dettaglio completo: [SETUP.md](SETUP.md). Versione breve:

```powershell
cd C:\LabOS\v1
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python launcher.py --all
```

Apri **http://localhost:5000**.

Cose che tipicamente cambiano da macchina a macchina: l'IP del robot Opentrons (`SiLA2/OpentronsSiLA2Server/config.yaml` → `robot.ip`), e se l'eseguibile del bridge Tecan è stato effettivamente compilato (vedi §9).

---

## 6. Tour pagina per pagina

Percorrile live, in quest'ordine — ricalca abbastanza fedelmente come un esperimento attraversa realmente il sistema.

### Dashboard (`/dashboard`)
Una card per strumento, stato in tempo reale, e un pulsante per eseguire direttamente un qualsiasi comando (con un form parametri se il comando ne richiede). È la vista "comando manuale" — utile per un comando singolo veloce (es. `Home` sul robot) senza costruire un intero workflow.

### Recipe Generator (`/batch`, raggiungibile anche come `/recipes` — stessa pagina)
Il modo normale per creare una ricetta: carica o seleziona un piano di pipettaggio Excel, raggruppa le colonne in fasi, scegli classe di liquido e condivisione reservoir per reagente, opzionalmente inserisci trattamenti su modulo (heater-shaker, temperatura). Clicca Generate. **Da sottolineare esplicitamente**: la stima dello scarto (waste) c'è, ma è dentro il pannello "Notes" comprimibile, non un numero in evidenza — aprilo, altrimenti i partecipanti penseranno che manchi.

**Come si calcola lo scarto, e quanto eccesso preparare** (questa domanda arriverà — è esattamente da qui che deriva quanta soluzione in eccesso serve):
Il sistema non aspira una volta per pozzetto. Con la strategia "Distribute", carica la pipetta **una volta sola** e dispensa in più pozzetti finché il volume caricato non finisce, poi ricarica ("viaggio" successivo). Ogni volta che ricarica, una piccola quantità fissa — il volume minimo reale della pipetta, es. 5 µL per la Flex 1-Channel 1000 µL (un dato di targa Opentrons vero, non un numero a caso) — resta nella punta come margine di sicurezza e non viene mai dispensata. Quello è lo scarto.
Quindi: più viaggi servono (più pozzetti destinazione, o una punta con capacità piccola rispetto al volume totale), più scarto c'è. Non è una percentuale fissa — dipende da quanti "ricarichi" servono per quel reagente specifico.
*Esempio concreto*: distribuire 50 µL a 24 pozzetti con la pipetta Flex 1000 µL (995 µL utilizzabili dopo aver riservato 5 µL di scarto) → circa 19 pozzetti per viaggio → 2 viaggi per coprire 24 pozzetti → scarto totale ≈ 10 µL, su un volume richiesto di 24 × 50 = 1200 µL (meno dell'1%). Con una pipetta diversa lo scarto per viaggio cambia di conseguenza (es. la Flex 50 µL ha un minimo di 1 µL).
*Quanto eccesso preparare davvero*: volume richiesto (somma dei pozzetti) + scarto stimato (mostrato per reagente nel pannello "Notes") + un margine tuo per il volume morto del serbatoio sorgente (quello il sistema **non** lo calcola — dipende dalla forma del contenitore, non dalla ricetta). Con la strategia "Transfer" (punta nuova per pozzetto) non c'è invece nessun accumulo di scarto per ricarico — più lenta, ma senza questo effetto.

### Hardware (`/hardware`)
Seleziona quale config HAL (layout del deck) è attiva. Persiste tra i riavvii. **Da sottolineare**: cliccare "Set Active" ora mostra un toast di avviso se il robot era offline e non ha effettivamente ricevuto l'aggiornamento (contro un semplice toast di successo quando invece l'ha ricevuto) — se vedi l'avviso, riapplica la config una volta confermato che il robot è online.

### Workflow Builder (`/workflows`)
Trascina strumenti su un canvas, imposta i parametri, collega le dipendenze, salva. Clicca Execute per lanciare. Una barra di stato globale compare in fondo a **ogni pagina** (non solo questa) con Pause/Resume/Abort ogni volta che qualcosa è in esecuzione. **Da sottolineare**: non esiste alcun blocco loop o IF/Else — LabOS supporta solo dipendenze lineari tra step, non controllo di flusso.

### Operator (`/operator`)
Dove un umano risponde al sistema: conferma uno step manuale, risponde a un Emergency Stop, ricarica i tip. **Da sottolineare**: "Add Log Note" qui scrive solo una voce di testo libero nel log di sessione — non crea un vero step di workflow, e la sua stessa finestra modale lo dichiara. Non affidarti a questo per nulla che debba condizionare l'esecuzione.

### Database (`/database`)
Il registro persistente e ricercabile di ogni esperimento, in due tab:
- **Plates** — il registro chimico. Ogni `ExecuteRecipe` Opentrons riuscito crea automaticamente una piastra con il contenuto della ricetta per pozzetto (reagente, volume, % di composizione). Cliccando una piastra si vede la griglia dei pozzetti; cliccando un pozzetto, il suo contenuto completo + le misure collegate. Se una piastra è stata analizzata con più di una tecnica (es. fluorescenza e assorbanza), compare un menu a tendina "Tecnica" — griglia e statistiche mostrano sempre una tecnica alla volta, mai una media tra le due. **Da sottolineare**: il pulsante "Aggiungi analisi" nel dettaglio piastra — lancia una nuova `RunMeasurement` Tecan con il PlateID di quella piastra già impostato, così collegare un'analisi successiva fatta giorni dopo (es. fluorescenza oggi, luminescenza domani, stessa piastra) è un click invece di copiare un ID a mano.
- **Runs & Log** — il registro di automazione: cosa ha fatto l'orchestratore, step per step, con rinomina/note/eliminazione e un pulsante "export for AI context" utile se si vuole dare i dati di una run a un LLM per un'analisi successiva.

**Da sottolineare**: questo collegamento è **automatico solo dentro l'esecuzione di un singolo workflow** — un comando manuale dal Dashboard, o un workflow lanciato un altro giorno, non eredita automaticamente una piastra precedente. Il pulsante "Aggiungi analisi" serve esattamente a coprire questo caso.

### Results (`/results`)
File di output grezzi (Excel, AnIML, CSV) — le letture effettive degli strumenti, separate dai metadati di Runs & Log.

### Settings (`/settings`)
Editor diretto di `lab_config.yaml`. Territorio da sviluppatore/amministratore — la maggior parte degli operatori non ne avrà bisogno.

---

## 7. Demo del Golden Path: un esperimento completo

Uno script che puoi seguire alla lettera dal vivo:

1. **Recipe Generator** — carica un piano Excel di esempio da `Library/Recipes/excel/`, configura una fase, genera una ricetta (e una HAL se non ne hai già una compatibile).
2. **Hardware** — seleziona e attiva la config HAL corrispondente; allestisci il deck fisico di conseguenza (tip rack, piastra, reagenti) mentre spieghi a cosa serve ogni slot.
3. **Workflow Builder** — costruisci (o carica uno già pronto) un workflow: Opentrons `ExecuteRecipe` → un `RequestOperatorTask` della Manual Station ("sposta la piastra al lettore") oppure, se il robot mobile è configurato per questa sessione, uno step `mobile` `ExecuteTask` (`TaskId`, `ExecutionMode: Normal`) che trasporta la piastra automaticamente → Tecan `RunMeasurement`. Clicca Execute.
4. **Osserva la status bar** mentre lo step Opentrons gira — spiega che il sistema sta proteggendo automaticamente da tip residui anche se non lo vedi accadere (§8).
5. **Pagina Operator** — quando parte lo step manuale, fai confermare il passaggio a un partecipante.
6. **Database** — a completamento, apri il tab **Runs & Log** per mostrare la timeline degli step, poi passa al tab **Plates** per mostrare il contenuto della ricetta di quella piastra e la misura collegata, e apri **Results** per mostrare il file di output Tecan effettivo.

Questo unico percorso tocca quasi tutte le pagine e la maggior parte dei meccanismi di sicurezza degni di nota.

---

## 8. Funzioni di sicurezza da spiegare

Funzionano automaticamente e sono facili da fraintendere come "non funzionanti" se nessuno le spiega:

- **Guardia anti-tip residuo (Opentrons)**: prima di ogni run, il sistema controlla la propria memoria su quali mount della pipetta potrebbero avere ancora un tip attaccato (il robot non ha un sensore fisico per questo) e forza automaticamente lo sgancio in caso di dubbio. A volte vedrai un movimento extra di drop-tip all'inizio di una run che non è nella ricetta che hai scritto — è questa guardia, non un bug.
- **Emergency Stop (pagina Operator)**: ferma la run corrente e tenta di sganciare i tip in sicurezza + fare l'home del robot. Se questa sequenza di sicurezza stessa fallisce, il sistema restituisce un avviso invece di dichiarare un successo silenzioso — prendi sul serio quell'avviso e controlla il robot fisicamente. Questo comando trasmette anche un tentativo di stop a ogni strumento rilevato, incluso il robot mobile se attivo.
- **Pause/Resume/Abort**: disponibili dalla status bar globale su qualsiasi pagina, non solo dal Workflow Builder.
- **Persistenza HAL + feedback di applicazione**: la config di deck attiva sopravvive a un riavvio, e la pagina Hardware ora mostra un chiaro toast di avviso se una selezione HAL non ha effettivamente raggiunto il robot (invece di sembrare tutto a posto silenziosamente).
- **Archivio run**: ogni run viene registrata automaticamente; non serve salvare nulla manualmente per avere un record permanente.
- **`Connect()` di Tecan**: ora rivalida sempre lo stato hardware reale — non dichiarerà più "connesso" dopo una disconnessione USB silenziosa.
- **I comandi di attivazione hardware del robot mobile** (`SetHardwareActivation`, `ActivateAllHardware`, ecc.) sono esplicitamente segnalati dal server stesso come fisicamente rilevanti — possono attivare motori/servo reali su braccio, pinza o base. Trattali con la stessa cautela di `Home` su Opentrons o `PlateIn`/`PlateOut` su Tecan: conferma che qualcuno sia fisicamente presente e stia osservando prima di chiamarli, non lanciarli "per vedere cosa succede".

---

## 9. Cosa NON fare / limiti attuali

Tratta questa sezione esplicitamente — previene la maggior parte delle domande "questo è rotto?" più avanti.

- **Non esiste controllo di flusso a loop o IF/Else** — il Workflow Builder supporta solo dipendenze lineari tra step. (Una versione precedente aveva blocchi Loop/IF-Else/Note nella toolbox che sembravano utilizzabili ma non erano realmente implementati; sono stati rimossi.)
- **"Add Log Note"** nella pagina Operator scrive una voce di testo libero nel log di sessione — non è uno step di workflow tracciato, e la finestra modale lo dichiara esplicitamente. Usa uno step di workflow `RequestOperatorTask` per qualsiasi cosa debba davvero condizionare l'esecuzione.
- **Offset del gripper** si può impostare solo modificando a mano il JSON di una ricetta — nessun controllo web UI ancora.
- **Verifica presenza liquido** (`VerifyLiquid`) ora *è* collegata alla UI per `Transfer`/`Distribute` — una spunta "Verify aspiration" per reagente nelle opzioni avanzate del Recipe Generator. Attivandola, la pipetta Flex sonda fisicamente il liquido prima di aspirare e ferma la run in modo pulito se non ne trova — nessun retry manuale da fare, sonda l'hardware stesso. Nota per `Distribute`: Opentrons riattiva questa sonda solo con un tip nuovo e pulito, quindi per accorgersi davvero se un pozzetto si svuota A METÀ operazione (non solo se è già vuoto all'inizio), il sistema usa automaticamente un tip nuovo ad ogni ricarico quando questa spunta è attiva — più tip consumati, run un po' più lenta, ma ogni ricarico viene controllato. Attivala solo sui reagenti dove serve davvero. I pozzetti di riserva vengono assegnati automaticamente quando la spunta è attiva — nessun numero da inserire: il sistema riserva abbastanza pozzetti extra nello stesso reservoir da coprire di nuovo il volume richiesto per quel reagente, e se il pozzetto in uso si svuota a metà run passa automaticamente al successivo — la run si ferma solo se anche tutti i pozzetti assegnati (primario + riserva) risultano vuoti. Controlla "Quantità richieste / note" dopo la generazione per vedere esattamente quali pozzetti sono stati assegnati, così sai dove caricare la scorta. (La vecchia variante solo-su-`Aspirate` standalone resta JSON-only, ma il Recipe Generator non genera mai quel comando comunque.)
- **Le stime di scarto** (waste) del Recipe Generator sono reali ma vengono mostrate solo come riga di testo dentro il pannello comprimibile "Notes" — ricorda ai partecipanti di espanderlo.

---

## 10. Troubleshooting di base — riferimento rapido

Dettaglio completo e altri scenari: [OPERATIONS.md](OPERATIONS.md#troubleshooting).

| Sintomo | Prima cosa da controllare |
|---|---|
| Uno strumento appare "offline" | Il suo processo/finestra terminale (o, per il robot mobile, il container Docker remoto) è ancora in esecuzione? Riavvialo — si riconnette automaticamente in circa 10s |
| Opentrons "could not connect to robot" | Fai `ping` all'IP del robot in `config.yaml`; controlla il touchscreen del robot stesso |
| Il bridge Tecan non risponde | Il software iControl è chiuso? Controlla Task Manager per l'eseguibile del bridge — dovrebbe riavviarsi automaticamente in pochi secondi |
| Un task del robot mobile fallisce o mostra "Server offline" | Controlla `docker ps`/`docker logs` su `192.168.11.22`; verifica che il marker ArUco target sia pulito e non ostruito |
| Un workflow non passa la validazione ("Instrument(s) not found") | Controlla l'ortografia e che lo strumento indicato sia attualmente online |
| Una ricetta fallisce contro la HAL attiva ("missing labware mapping") | Conferma che la HAL corretta sia attiva **e sia stata effettivamente applicata al robot** — la pagina Hardware ora avvisa esplicitamente se non lo è stata |
| Il conteggio dei tip sembra sbagliato | La guardia automatica anti-tip potrebbe aver forzato uno sgancio che non ti aspettavi — controlla il log archiviato della run prima di dare per scontato un bug |
| Non trovi una piastra/run che sai esistere | Assicurati di essere sulla pagina **Database** (backend SQLite), sul tab giusto (**Plates** vs **Runs & Log**) |
| `RunMeasurement` Tecan fallisce con "PlateType not found" | Normale — quella piastra non è ancora registrata in `PlateTypes.json`, oppure lascia `PlateType` vuoto per usare la piastra di default del protocollo. Vedi [SERVERS.md](SERVERS.md#mdfx-protocol-files) |

---

## 11. Dove trovare le cose

| Cosa | Dove |
|---|---|
| Indice completo della documentazione | [README.md](README.md) |
| Architettura del sistema | [ARCHITECTURE.md](ARCHITECTURE.md) |
| Ogni comando SiLA2, per server | [SERVERS.md](SERVERS.md) |
| API REST/WebSocket | [API_REFERENCE.md](API_REFERENCE.md) |
| Formato ricetta/HAL | [HAL_SYSTEM.md](HAL_SYSTEM.md) |
| Formato JSON dei workflow | [WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md) |
| Aggiungere un nuovo strumento | [ADDING_NEW_INSTRUMENT.md](ADDING_NEW_INSTRUMENT.md) |
| Log applicativi | Cartella `logs/` di ogni server — non esiste più una pagina di log live nel browser, era effimera ed è stata rimossa |
| Registri permanenti degli esperimenti | Pagina **Database** (tab Plates + Runs & Log) / cartella `Results/` / `run_archive/` di ogni run Opentrons |

---

## 12. Riferimento rapido

```
Avvia tutto:               cd v1 && python launcher.py --all
Interfaccia web:            http://localhost:5000
Opentrons SiLA2:            localhost:50302
Tecan SiLA2:                localhost:50051   (bridge interno: 50055)
Manual Station SiLA2:       localhost:50500
Robot mobile (remoto):      192.168.11.22:50053
```
