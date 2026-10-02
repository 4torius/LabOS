# Test di affidabilita dei task del robot

`scripts/robot_reliability_runner.py` misura quanto e affidabile **ogni singolo
task del robot** in funzione di dove si trova la base. E un programma esterno:
non modifica LabOS e usa solo le API REST pubbliche del webapp.

## Sequenza completa, dall'inizio

### 0. Attivare il venv — in ogni finestra di PowerShell

Il `python` di sistema non ha le dipendenze del progetto e fallisce con
`ModuleNotFoundError: No module named 'yaml'`. Serve il venv:

```powershell
cd C:\LabOS\v1
.\.venv\Scripts\Activate.ps1
```

Il prompt diventa `(.venv) PS C:\LabOS\v1>`. Se l'attivazione dovesse dare
problemi, si puo sempre chiamare l'eseguibile per esteso senza attivare nulla:
`C:\LabOS\v1\.venv\Scripts\python.exe labos.py status`.

### 1. Controlli preliminari — non si muove niente

```powershell
python labos.py status
python labos.py validate Library\Workflows\Pick_place_OT.workflow.json
```

Il robot mobile deve risultare raggiungibile. Se non lo e, fermarsi qui.

### 2. Avviare LabOS, in una finestra separata

```powershell
cd C:\LabOS\v1
.\.venv\Scripts\Activate.ps1
python webapp\app.py
```

Aprire `http://localhost:5000` e **tenere la pagina aperta**: e da li che si fa
Abort quando un task sbaglia.

### 3. Prova a vuoto — da qui il robot si muove

Piastra sulla dima, area sgombra, mano sull'emergenza. Una ripetizione sola:

```powershell
python scripts\robot_reliability_runner.py Library\Workflows\Pick_place_OT.workflow.json `
  --position 0,0,0 --repeats 1 --experiment-id prova
```

Durante questa prova conviene **far fallire una presa di proposito** (togliere
la piastra dalla dima) e guardare se il robot dichiara il fallimento o se
riporta comunque "ok". La risposta si legge nella colonna `robot-cieco` del
report e decide se `--unattended` sara mai utilizzabile.

Poi si buttano i dati di prova: `Remove-Item Results\robot_reliability\prova_*`

### 4. La mappa

Una sessione per posizione, spostando la base a mano fra una e l'altra:

```powershell
python scripts\robot_reliability_runner.py Library\Workflows\Pick_place_OT.workflow.json `
  --position 0,0,0 --repeats 30
python scripts\robot_reliability_runner.py Library\Workflows\Pick_place_OT.workflow.json `
  --position 5,0,0 --repeats 30
```

Stesso `--experiment-id` (default `reliability_map`) = una sola mappa.

Nota sul numero di ripetizioni: con 30 prove il limite inferiore di Wilson non
arriva a 0.90 nemmeno con 30 successi su 30. Trenta ripetizioni servono a
**trovare il bordo**, non a certificare una posizione: per quello ne servono
100, e solo sui punti che contano.

### 5. Rileggere la mappa

```powershell
python scripts\robot_reliability_runner.py --report-only
```

## Il workflow

Deve essere un **ciclo chiuso di soli task mobile**: alla fine la cella torna
nello stato iniziale, altrimenti la seconda ripetizione parte da una dima
vuota. `Library/Workflows/Pick_place_OT.workflow.json`:

1. `pick_from_dima`
2. `place_on_OT`
3. `pick_from_opentrons` (riprende la piastra e la riappoggia sulla dima)

Niente ricette, niente misure: si stanno caratterizzando i task del robot.

Niente task di navigazione (`go_to_*`): il robot si riporterebbe alla propria
posa nominale, annullando lo spostamento della base che si vuole misurare.

Attenzione al `DependsOn` quando si modificano gli step. Rinumerando uno step
senza aggiornare le dipendenze si ottiene facilmente uno step che dipende da se
stesso: LabOS lo rifiuta in validazione e il run fallisce subito.

## Chi decide se un task e riuscito

**L'operatore, non il robot.** `ExecuteTask` riporta che il controllore ha
eseguito la traiettoria, non che la piastra sia arrivata dove doveva: una pinza
che si chiude a vuoto e completa il movimento riporta successo.

Quindi dopo ogni run il runner mostra cosa dice il robot e chiede il verdetto:

```
  Il robot riporta:
    1. pick_from_dima         ok
    2. place_on_OT            ok
    3. pick_from_opentrons    ok
  Verdetto? [Invio=tutto ok | numeri dei task falliti (es. 2 o 1,3) | x=prova da scartare]
  >
```

Un tasto solo nel caso normale. Si digitano i numeri solo quando qualcosa e
andato storto. `x` scarta la prova (hai urtato tu qualcosa, e stato interrotto
per una causa esterna): resta nel journal ma fuori dalle percentuali.

Le due versioni sono conservate entrambe, `robot_status` e `operator_status`.
Dove divergono vince l'operatore, e la divergenza viene contata:
`robot_reported_ok_but_failed` dice quanto e cieco l'autodiagnosi del
controllore. Se quel numero e alto, la mappa **non** si puo costruire senza
operatore.

Con `--unattended` non viene chiesto nulla e si prende per buono quello che
dice il robot. Da usare solo dopo aver verificato che il robot i fallimenti li
riporta davvero.

## La posizione della base

`--position` e l'offset **assoluto rispetto al nominale**, non uno spostamento
relativo alla posizione precedente. Se lo si dimentica il runner lo chiede: non
assume mai 0,0,0, perche una sessione archiviata per sbaglio al nominale
corrompe la mappa in silenzio.

Facoltativi: `--measured 4.8,0.2,0.5` se si e misurato l'offset reale, e
`--note "..."` per un commento sulla sessione. Servono solo se interessa
distinguere l'offset voluto da quello ottenuto.

## Se un task fallisce

Lascia finire o **fermalo con l'Abort dalla pagina LabOS**, poi dichiara il
task fallito al prompt. L'abort software chiede agli strumenti di fermarsi ed e
la via normale per dichiarare un fallimento.

Il pulsante di **emergenza** va usato solo per pericolo fisico reale. Dal punto
di vista del runner un E-stop e invisibile: il controllore smette di rispondere
e il run resta appeso finche non fai Abort dalla pagina LabOS, o finche non
scade `--run-timeout` (default 30 min) e il runner chiede l'abort da solo. Dopo
un E-stop: verifica l'area, completa reset e riarmo previsti da Exsensia e dal
controllore, poi rispondi al prompt.

Dopo ogni prova fallita il runner si ferma e chiede se continuare: la cella e
in uno stato non noto, e vanno rimessi piastra, pinza e deck nella condizione
di partenza prima di proseguire.

## Come vengono contati i task

**Il denominatore e il numero di tentativi, non di run.** In un ciclo
sequenziale, se `pick_from_dima` fallisce i task successivi non vengono mai
tentati: sono registrati `not_reached` e restano fuori dal denominatore.
Dividere per il numero di run farebbe apparire inaffidabile l'ultimo task del
ciclo per colpa del primo.

**Il numero di riferimento e il tasso al primo colpo** (`first_pass_rate`): un
task che riesce solo al secondo tentativo non e affidabile, e recuperato. Il
report mostra anche `rate_including_retries` e `needed_retry`.

**Le durate**: mediana e p95 per task. Ai bordi della mappa il tempo si allunga
e diventa piu variabile **prima** che il tasso di successo cali: e
l'indicatore anticipato del limite.

## Soglia

La percentuale osservata non basta, si usa l'intervallo di Wilson al 95%:
`safe_confirmed` se il limite inferiore supera la soglia, `unsafe_confirmed` se
quello superiore ci sta sotto, `undetermined` se l'intervallo la attraversa.

Con soglia 90% servono **96 successi su 100** perche una posizione risulti
`safe_confirmed`. A 95/100 resta `undetermined`.

## Dove finiscono i dati

In `Results/robot_reliability/` (cioe `C:\LabOS\v1\Results\robot_reliability\`),
cambiabile con `--output`. La cartella e in `.gitignore`.

- `reliability_map_journal.jsonl`: append-only, la fonte di verita, una riga
  per run con i task annidati;
- `reliability_map_trials.csv`: un run per riga;
- `reliability_map_tasks.csv`: un task per riga, con la posizione della base
  ripetuta su ogni riga — **basta questo file per ricostruire la mappa**;
- `reliability_map_summary.json`: aggregati per posizione e per task.

Il runner **solo appende** al journal; CSV e summary sono ricostruiti
rileggendolo dall'inizio. Quindi la mappa resta completa fra sessioni diverse,
sopravvive a un crash, e si rigenera in qualsiasi momento:

```powershell
python scripts/robot_reliability_runner.py --report-only
```

## Automazione del movimento

Quando il protocollo del controllore sara noto:

```powershell
python scripts/robot_reliability_runner.py Library/Workflows/Pick_place_OT.workflow.json `
  --positions "0,0,0;5,0,0;-5,0,0" `
  --move-command "ssh controller-pc python move_base.py {dx} {dy} {dtheta}" --unattended
```

Il template riceve offset **assoluti** rispetto al nominale e deve bloccare il
ritorno finche il controllore ha confermato il movimento. Non usare RDP come
protocollo di comando.
