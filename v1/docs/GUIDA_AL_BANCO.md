# LabOS al banco

**Procedura operativa — Laboratorio Bicocca**

Dall'accensione al risultato: cosa fare, in che ordine, e i punti dove il sistema
si ferma se salti un passaggio. Opentrons Flex, Tecan M200 Pro, robot mobile
GoFaGo.

> Esiste anche una versione HTML di questa guida, apribile con doppio clic senza
> nessun server: [`GUIDA_AL_BANCO.html`](GUIDA_AL_BANCO.html).

**Indice**

1. [Avviare il sistema](#fase-1--avviare-il-sistema)
2. [Connettere il Tecan](#fase-2--connettere-il-tecan)
3. [Preparare l'esperimento](#fase-3--preparare-lesperimento)
4. [Eseguire](#fase-4--eseguire)
5. [Durante e dopo](#fase-5--durante-e-dopo)
6. [Il robot mobile](#fase-6--il-robot-mobile)
7. [Riferimento rapido](#riferimento-rapido)

---

## Fase 1 — Avviare il sistema

Due minuti. Il launcher accende i server degli strumenti e l'interfaccia web.

1. Accendi prima **gli strumenti fisici**: Opentrons Flex e Tecan M200. Il Tecan
   deve essere acceso e collegato via USB *prima* che parta il suo server,
   altrimenti non trova nulla da enumerare.
2. Doppio clic su `LabOS_launcher.bat` sul desktop.
3. Premi `1` e Invio — **START ALL**. Ferma tutto quello che era rimasto acceso,
   riavvia i server SiLA2, avvia la webapp e apre il browser da solo.
4. Il browser si apre su `http://localhost:5000`. Se non si apre, scrivilo a mano.
5. Vai sulla **Dashboard** e controlla che ogni strumento abbia la card **verde**.
   Uno strumento offline qui è uno strumento che non risponderà nemmeno dentro
   un workflow.

**Le altre voci del menu.** `2` solo i server, `3` solo la webapp, `4` aggiorna
lo stato, `5` apre `lab_config.yaml`, `6` ferma tutto. A fine giornata usa `6`:
chiudere solo la finestra lascia i server in esecuzione.

> **Se lo salti:** uno strumento che non è online al momento dell'avvio non viene
> scoperto, e i suoi comandi non compaiono da nessuna parte nell'interfaccia.

---

## Fase 2 — Connettere il Tecan

Obbligatorio a ogni avvio. Il lettore non si connette da solo.

1. **Chiudi iControl e Magellan.** Tengono la porta USB in esclusiva: finché uno
   dei due è aperto, il server non riesce a connettersi. Chiudere la finestra non
   basta sempre, verifica che il processo sia terminato.
2. Dashboard → card **Tecan M200 Pro** → comando `Connect`.
3. Lascia `ConnectionString` su `usb` e conferma.
4. La proprietà `IsConnected` passa a vero. Da qui in poi `PlateIn`,
   `RunMeasurement`, `PlateOut` funzionano.

### I modi di connessione, e quando servono

| Valore | Cosa fa | Quando usarlo |
|--------|---------|---------------|
| `usb` | Prova la connessione memorizzata, poi una scansione nuova | Sempre. È l'unico sicuro se non c'è nessuno davanti al PC |
| `ask` | Come sopra, poi apre la finestra di scelta strumento | Quando `usb` fallisce e tu sei lì |
| `dialog` | Apre subito la finestra di scelta | Più lettori collegati, vuoi scegliere tu |
| `sim` | Simulatore Tecan | Provare un workflow senza hardware |

> **Attenzione.** `ask` e `dialog` aprono una finestra sul PC del laboratorio e
> **restano in attesa per sempre** finché qualcuno non risponde. Non usarli in un
> workflow che gira di notte.

**Se hai spostato il lettore su un'altra porta USB:** `usb` se ne accorge e lo
ritrova da solo. Non serve fare altro.

---

## Fase 3 — Preparare l'esperimento

Quattro pezzi, e devono combaciare tra loro: la ricetta, la configurazione
hardware, il deck fisico, l'analisi Tecan.

### 3a · La ricetta Opentrons

1. Vai su **Recipe Generator** (`/recipes`, la stessa pagina di `/batch`).
2. Carica o seleziona un piano di pipettaggio Excel da `Library/Recipes/excel/`.
3. Raggruppa le colonne in fasi, scegli la classe di liquido per ogni reagente,
   decidi la condivisione dei serbatoi, aggiungi eventuali trattamenti su modulo.
4. Genera. La ricetta finisce in `Library/Recipes/`.
5. **Apri il pannello "Notes"**, è chiuso di default: lì dentro c'è la stima
   dello scarto per reagente, cioè quanto in eccesso devi preparare davvero.

### 3b · La configurazione hardware (HAL)

> **Il pezzo che manca più spesso.** La ricetta non nomina slot, nomina
> **etichette logiche**: `MyTips`, `TargetPlate`, `Source_1`. È la configurazione
> hardware a dire in quale slot sta ciascuna. Senza quella giusta attiva la
> ricetta non parte, e se è attiva ma sbagliata il robot va a pescare nello slot
> sbagliato.

1. Il Recipe Generator può generare una configurazione insieme alla ricetta. Usa
   quella se non ne hai già una compatibile.
2. Vai su **Hardware** (`/hardware`) e clicca **Set Active** sulla configurazione
   giusta. La scelta resta anche dopo un riavvio.
3. Se compare un avviso che il robot era offline, riapplica la configurazione
   dopo averlo visto online: l'attivazione non gli è arrivata.

### 3c · Allestire il deck e verificarlo

Il deck fisico deve corrispondere alla configurazione attiva, slot per slot.
Questo è `01_SPAAC`, per darti il formato:

|   | 1 | 2 | 3 |
|---|---|---|---|
| **A** | `HeaterShaker` — modulo | libero | `MyTrash` — cestino |
| **B** | libero | libero | `MyTips` — tip rack 1000 µL |
| **C** | libero | libero | libero |
| **D** | `Source_1` — 24 pozzetti | `TargetPlate` — 96 pozzetti | libero |

La colonna 4 (`A4`–`D4`) è l'area di staging, raggiungibile dal gripper. I moduli
dichiarati devono essere **fisicamente collegati**: se la configurazione dichiara
un heater-shaker che non c'è, il comando si rifiuta di partire e te lo dice.

1. Metti tip rack, piastre e reagenti negli slot che la configurazione indica.
2. Dalla Dashboard lancia `CaptureImage` sull'Opentrons: scatta una foto del deck
   senza muovere niente, e te la ritrovi nella pagina Results. Usala per
   confrontare quello che vedi con quello che il software crede.
3. Lancia `ValidateRecipe` con il nome della tua ricetta. Ti dice esattamente
   quali etichette mancano nella configurazione attiva, **senza eseguire niente**.
   Costa cinque secondi e ti evita un run fallito a metà.
4. Se hai messo un tip rack nuovo, lancia `RefillTipRack`: il server conta le
   punte usate e altrimenti continua a crederle consumate.

### 3d · L'analisi Tecan

1. Apri **iControl** e costruisci il metodo di misura: lunghezze d'onda, tipo di
   piastra, tutto.
2. Salvalo come `.mdfx` dentro `Library/Analysis/`.
3. **Chiudi iControl** prima di tornare a usare il Tecan da LabOS, vedi Fase 2.
4. Nel comando `RunMeasurement` il protocollo si sceglie da una **tendina**:
   l'elenco viene letto da `Library/Analysis/` e non si scrive niente a mano. Se
   il file nuovo non compare, ricarica la pagina.

> **Se lo salti:** i parametri della misura vivono solo nel file `.mdfx`. LabOS
> non ha modo di impostare una lunghezza d'onda, sceglie solo quale protocollo
> far girare.

---

## Fase 4 — Eseguire

### Comando singolo

Dashboard → card dello strumento → clic sul comando. Se ha parametri si apre un
form, con i menu a tendina già riempiti (le ricette, le configurazioni, il
labware sul deck, gli slot liberi). È la via giusta per un gesto isolato: una
connessione, una foto, un `Home`, una misura al volo.

> **Muove hardware.** I comandi la cui descrizione comincia con **MOVES HARDWARE**
> muovono davvero qualcosa. Su Opentrons: `Home`, `ExecuteRecipe`, `MoveLabware`,
> `ShakeModule`, `SetModuleLatch`, `SetModuleTemperature`, `ResumeRun`,
> `AbortRun`. Guarda il deck prima di premere.

### Comporre un workflow

1. Vai su **Workflows** (`/workflows`).
2. **Aggiungi uno step** scegliendo lo strumento. Lo step compare nella lista a
   sinistra.
3. **Scegli l'azione** nel pannello dei parametri: è la lista dei comandi che
   quello strumento espone davvero, non una lista fissa.
4. **Compila i parametri.** Dove c'è una tendina, usala: i valori arrivano dalla
   libreria e dalla configurazione attiva, quindi sono valori che esistono.
5. Ripeti per ogni step. Un ordine tipico: Opentrons `ExecuteRecipe` → trasporto
   della piastra (step manuale oppure robot mobile) → Tecan `RunMeasurement`.
6. **Le dipendenze sono facoltative**, vedi sotto. Se ti servono, nel pannello
   dello step ci sono le caselle `DependsOn`.
7. **Salva.** Il workflow finisce in `Library/Workflows/` come `.workflow.json`.
8. **Execute.** Compare una barra di stato in fondo a **ogni** pagina, con Pause,
   Resume e Abort. Puoi navigare altrove mentre gira.

### Le dipendenze: quando servono e quando no

**Un workflow senza nessuna dipendenza funziona.** Gli step girano uno dopo
l'altro, nell'ordine in cui li hai messi. Per una sequenza lineare — prepara,
trasporta, misura — non devi spuntare niente.

Le dipendenze servono a una cosa sola, ma importante: **far saltare gli step che
non hanno più senso** quando un prerequisito fallisce. Se la misura dipende dalla
preparazione e la preparazione fallisce, la misura viene saltata invece di
partire su una piastra vuota. Senza dipendenze, il workflow si ferma comunque al
primo errore, ma il sistema non sa *quali* step erano collegati a quale.

**Esecuzione in parallelo.** È **spenta** (`workflow.parallel_execution: false`
in `lab_config.yaml`), ed è una scelta globale, non per singolo workflow. Con il
parallelo spento ogni step aspetta che finisca il precedente, punto: è il
comportamento che vuoi, e non devi fare niente per averlo.

Se un giorno lo accendi, gli step **senza dipendenze tra loro partono insieme**.
È allora che `DependsOn` diventa obbligatorio, perché è l'unica cosa che
impedisce alla misura di partire mentre il robot sta ancora pipettando.

Non esistono cicli né condizioni. LabOS fa sequenze con dipendenze, non controllo
di flusso.

### Quanto tempo aspetta uno step

Non c'è niente da impostare, e il comportamento non è lo stesso per tutti i
comandi.

- **I comandi lunghi non hanno limite.** `ExecuteRecipe`, `RunMeasurement`,
  `MoveLabware`, `Home` finiscono quando lo strumento ha finito, che sia un
  minuto o tre ore. È voluto: un run da due ore non deve essere ucciso a metà
  perché un contatore è scaduto.
- **I comandi brevi hanno un limite di 600 secondi**
  (`workflow.default_timeout`). Un comando che per progetto risponde subito e
  invece non risponde è un guasto, e dieci minuti bastano a riconoscerlo.
- Se uno step resta fermo senza motivo, guarda prima la pagina **Operator**:
  quasi sempre sta aspettando una conferma, non lo strumento.

---

## Fase 5 — Durante e dopo

### Mentre gira

- **Operator** (`/operator`) — qui l'umano risponde al sistema. Se un workflow ha
  uno step manuale, o chiede di ricaricare le punte, **resta fermo finché
  qualcuno non conferma qui**. Timeout: un'ora per un intervento, dieci minuti
  per un'azione manuale o un refill. Se nessuno guarda questa pagina, il workflow
  scade.
- **Barra di stato** — in fondo a ogni pagina mentre qualcosa è in esecuzione.
  Pause, Resume, Abort. L'Abort arriva davvero allo strumento, non si limita a
  smettere di guardare.
- **EmergencyStop** — sulla card Opentrons della Dashboard, per fermare tutto
  subito.

### Dopo

- **Database** (`/database`) → tab **Plates**: ogni `ExecuteRecipe` riuscito crea
  una piastra con il contenuto pozzetto per pozzetto. Cliccando un pozzetto vedi
  reagenti, volumi e le misure collegate.
- Tab **Runs & Log**: la cronologia degli step dell'orchestratore, con rinomina,
  note ed export.
- **Results** (`/results`): i file grezzi degli strumenti, Excel e AnIML, più le
  foto del deck.

> **Collegare una misura a una piastra.** Il collegamento automatico avviene solo
> **dentro un singolo workflow**. Un comando manuale dalla Dashboard, o
> un'analisi fatta giorni dopo, non eredita niente. Per quel caso usa **"Aggiungi
> analisi"** nel dettaglio della piastra: lancia una `RunMeasurement` con il
> `PlateID` giusto già impostato.

---

## Fase 6 — Il robot mobile

GoFaGo: base Robotnik, braccio ABB, gripper OnRobot. Gira su un suo PC Linux, non
su quello del laboratorio.

### Localizzare il robot

1. Collegati all'**hotspot del robot**.
2. Apri **AnyDesk** verso `192.168.11.22`.
3. Sul desktop remoto apri **Chrome** e vai al sito di **Proxima**, è tra le
   *fast action*.
4. Accedi con `admin` / `admin`.
5. Vai sulla pagina **Mission** e localizza il robot:
   - se è fermo su un punto noto della mappa, seleziona il **waypoint** e
     localizza da lì;
   - altrimenti fallo **a mano**, trascinando la posa e verificando che la
     scansione laser combaci con i muri della mappa. Se i laser non combaciano,
     la navigazione andrà a sbattere.

> **Localizzare è solo il prerequisito.** Serve perché il robot sappia dov'è, ma
> non basta per fargli fare qualcosa da LabOS: mancano l'attivazione
> dell'hardware e i task.

### Poi, da LabOS

1. Dashboard → card **Mobile Robot**. Se è grigia, il container non risponde:
   controlla che sia acceso e che tu sia sulla sua rete.
2. **Attiva l'hardware** con `ActivateAllHardware`. All'accensione è tutto
   disattivato. Se preferisci pezzo per pezzo usa `SetHardwareActivationByRole`
   con `MobileBase`, `Arm`, `Gripper`, `Camera`: la base va attivata prima del
   braccio, e `ActivateAllHardware` lo garantisce da solo.
3. Lancia `RefreshTasks`: riporta dal robot la lista dei task e la salva in
   `Library/MobileTasks/`. Senza questo la tendina dei task nel workflow builder
   è vuota.
4. Aggiungi uno step `ExecuteTask` per ogni task e scegli il `TaskId` dalla
   tendina. Il parametro `ExecutionMode` esiste ancora nell'interfaccia ma il
   server attuale **lo ignora**: lascialo su `Normal` e non aspettarti che
   `DryRun` simuli qualcosa.

> **Un trasporto non è uno step solo.** Ogni task fa una cosa sola. Portare una
> piastra dall'Opentrons al Tecan sono **quattro step** di workflow, in
> quest'ordine:
>
> `go_to_opentrons` → `pick_from_opentrons` → `go_to_tecan` → `place_on_TC`
>
> Al contrario, dal Tecan all'Opentrons: `go_to_tecan` → `pick_from_tecan` →
> `go_to_opentrons` → `place_on_OT`.

### I task che esistono davvero

Letti dal robot. La piastra viaggia appoggiata alla **dima** a bordo: prenderla
da uno strumento significa anche posarla sulla dima, e posarla su uno strumento
significa anche riportare il braccio a riposo.

| Task | Cosa fa |
|------|---------|
| `go_to_opentrons` | Naviga fino all'Opentrons |
| `go_to_tecan` | Naviga fino al Tecan |
| `pick_from_opentrons` | Prende la piastra dall'Opentrons e la appoggia sulla dima |
| `pick_from_tecan` | Prende la piastra dal Tecan |
| `place_on_OT` | Posa la piastra sull'Opentrons, poi il braccio torna a riposo |
| `place_on_TC` | Posa la piastra sul Tecan, poi il braccio torna a riposo |
| `pick_from_dima` | Riprende la piastra dalla dima, se è rimasta lì |
| `docking` | Torna alla stazione di ricarica |
| `TestFra`, `BaseFra` | Taratura dei frame. Non servono all'uso normale |

### Prima di farlo muovere davvero

- **Marker ArUco puliti e non ostruiti.** Le traiettorie del braccio sono
  relative al marker della postazione, non alla mappa. Marker sporco o coperto
  significa presa sbagliata.
- **Batteria sopra il 20%**, altrimenti si rifiuta di operare.
- Il robot si ferma da solo se trova un ostacolo entro **30 cm**. Velocità
  massime: 0,5 m/s la base, 0,3 m/s il braccio.
- L'hardware a bordo: braccio **ABB GoFa5**, gripper **OnRobot 2FG7**, camera
  **Intel RealSense D415**, base **Robotnik Summit-XL Steel**. Nella lista
  compaiono con i loro nomi interni; usa i ruoli e non dovrai conoscerli.

**Se un task fallisce:** la causa più frequente è il marker. Poi la
localizzazione. Poi il container fermo sul PC remoto.

---

## Riferimento rapido

### Indirizzi e cartelle

| Cosa | Dove |
|------|------|
| Webapp | `http://localhost:5000` |
| Opentrons Flex | `localhost:50302` → robot su `:31950` |
| Tecan M200 Pro | `localhost:50051` (USB) |
| Robot mobile | `192.168.11.22:50053` · AnyDesk sullo stesso indirizzo |
| Ricette | `Library/Recipes/` |
| Piani Excel | `Library/Recipes/excel/` |
| Configurazioni hardware | `Library/HardwareConfig/` |
| Analisi Tecan | `Library/Analysis/` · `.mdfx` |
| Workflow | `Library/Workflows/` · `.workflow.json` |
| Task robot mobile | `Library/MobileTasks/` |
| Risultati e foto | `Results/` |

### Quando qualcosa non va

| Sintomo | Prima cosa da controllare |
|---------|---------------------------|
| Tecan: `Connect` fallisce | iControl aperto. Poi: lettore acceso, cavo USB. Poi prova `ask`. |
| Strumento grigio sulla Dashboard | Era acceso quando hai lanciato il launcher? Rifai `1`. |
| La ricetta fallisce subito | `ValidateRecipe`. Quasi sempre la configurazione hardware non è quella giusta. |
| Comando su modulo rifiutato | Il modulo è dichiarato in configurazione ma non collegato, o viceversa. Leggi `ModulesStatus`. |
| Il workflow è fermo e non fa niente | Pagina **Operator**: sta aspettando una conferma. |
| Il robot mobile dice "Server offline" | Container fermo sul PC remoto, oppure non sei sulla sua rete. |
| Task del robot fallito | Marker ArUco sporco o coperto. Poi la localizzazione. |
| "Non ci sono punte" | Hai cambiato un rack senza `RefillTipRack`. |

### La camera del Flex

`CaptureImage` fotografa il deck senza muovere il robot, e accende e rispegne la
camera da sola. **Il video non è possibile** su questo Flex: il firmware dichiara
uno stream ma non lo trasmette mai.

---

Procedura verificata sul banco contro il Flex, il Tecan e il robot mobile reali.
Per il dettaglio tecnico di ogni comando: [SERVERS.md](SERVERS.md),
[WORKFLOW_SYSTEM.md](WORKFLOW_SYSTEM.md), [HAL_SYSTEM.md](HAL_SYSTEM.md).
