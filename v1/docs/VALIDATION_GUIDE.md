# Guida alla Validazione Sperimentale

Questa guida ti aiuterà a eseguire la validazione sperimentale completa quando arriverà il GoFaGo.

---

## 1. Pre-requisiti

### Hardware
- [ ] GoFaGo installato e calibrato (RB Kairos + ABB GoFa)
- [ ] Opentrons Flex operativo
- [ ] Tecan M200 Pro operativo
- [ ] Network configurata (tutti i device sulla stessa subnet)
- [ ] ArUco markers installati su ogni workstation

### Software
- [ ] SiLA2 servers tutti funzionanti
- [ ] ROS navigation stack configurato
- [ ] Mappa del laboratorio generata (SLAM)
- [ ] Orchestrator attivo

### Materiali per test
- [ ] Microplates 96-well (almeno 20 per i test)
- [ ] NIST-traceable neutral density filters (per Tecan)
- [ ] Bromophenol blue dye solution (per dilution test)
- [ ] Laser tracker o motion capture system (opzionale, per navigation accuracy)
- [ ] Bilancia analitica (0.1 mg resolution)

---

## 2. Fase 1: Validazione Mobile Robot (Settimana 1)

### 2.1 Navigation Accuracy Test

**Procedura:**
1. Definisci 4 waypoints (uno per workstation) nel sistema di coordinate del robot
2. Per ogni waypoint:
   - Comanda il robot a navigare verso la posizione
   - Misura la posizione raggiunta (laser tracker, motion capture, o metro)
   - Registra errore di posizione (distanza euclidea dal target)
   - Registra errore di orientamento
3. Ripeti 10 volte per ogni waypoint

**Template dati:**
```
| Workstation | Trial | Target X | Target Y | Achieved X | Achieved Y | Position Error (mm) | Orientation Error (°) |
|-------------|-------|----------|----------|------------|------------|--------------------|-----------------------|
| Opentrons   | 1     | ...      | ...      | ...        | ...        | ...                | ...                   |
```

**Success criteria:**
- Mean position error ≤ 20 mm
- Max position error ≤ 30 mm
- Mean orientation error ≤ 2°

### 2.2 Visual Servoing Test

**Procedura:**
1. Naviga verso workstation (stop a ~50cm)
2. Attiva visual servoing per ArUco marker
3. Misura posizione finale relativa al marker
4. Ripeti 10 volte per workstation

**Success criteria:**
- Position error after servoing ≤ 5 mm
- Convergence time ≤ 10 s

### 2.3 Pick & Place Test

**Procedura:**
1. Posiziona una microplate sulla workstation sorgente
2. Esegui comando `transport_plate` verso destinazione
3. Verifica:
   - Plate picked up successfully (grip sensor)
   - No collisions during transport
   - Plate placed correctly (seated in holder)
4. Ripeti 20 volte per ogni coppia source-destination

**Template dati:**
```
| Source | Destination | Trial | Pick OK | Transport OK | Place OK | Notes |
|--------|-------------|-------|---------|--------------|----------|-------|
| OT     | Tecan       | 1     | Yes     | Yes          | Yes      |       |
```

**Success criteria:**
- Success rate ≥ 95%
- Zero plate drops

---

## 3. Fase 2: Integration Testing (Settimana 2)

### 3.1 Workflow 1: Simple Transfer and Read

**Workflow:**
```json
{
  "steps": [
    {"device": "opentrons", "action": "transfer", "params": {...}},
    {"device": "mobile", "action": "transport", "params": {"from": "opentrons", "to": "tecan"}},
    {"device": "tecan", "action": "measure_absorbance", "params": {"wavelength": 450}},
    {"device": "system", "action": "export", "params": {"format": "csv"}}
  ]
}
```

**Procedura:**
1. Prepara source plate con dye solution
2. Avvia workflow
3. Monitor esecuzione
4. Verifica:
   - Tutti gli step completati
   - Results file generato
   - Dati corretti
5. Ripeti 10 volte

**Success criteria:**
- Success rate ≥ 95%
- Execution time < 15 min

### 3.2 Workflow 2: Serial Dilution with Analysis

**Workflow più complesso con branch paralleli.**

**Success criteria:**
- Success rate ≥ 90%
- Parallel execution observed
- Correct dependency resolution

### 3.3 Workflow 3: Multi-Step with Manual Intervention

**Include manual station per testare HRC.**

**Success criteria:**
- Success rate ≥ 85%
- Operator notification received
- Proper pause-on-confirmation behavior

---

## 4. Fase 3: Hydrogel Case Study (Settimana 3)

### 4.1 Stock Solution Preparation

**Prepara:**
- Polymer solution (e.g., PEG-diacrylate): X% w/v stock
- Crosslinker solution: Y mM stock
- Initiator solution (e.g., Irgacure 2959): Z% w/v
- PBS buffer

### 4.2 Formulation Matrix Design

**Design della matrice:**
| Parameter | Levels | Values |
|-----------|--------|--------|
| Polymer conc. | 4 | 5%, 8%, 12%, 15% w/v |
| Crosslinker ratio | 4 | 1:10, 1:20, 1:35, 1:50 |
| Initiator conc. | 3 | 0.1%, 0.3%, 0.5% |

Total: 4 × 4 × 3 = 48 formulations × 2 replicates = 96 wells

### 4.3 Recipe Creation

Crea recipe JSON per Opentrons che:
1. Dispensa polymer alle concentrazioni corrette
2. Aggiunge crosslinker
3. Aggiunge initiator
4. Miscela

### 4.4 Workflow Execution

```
1. [Manual] Prepare stocks → Confirm in UI
2. [Auto] Opentrons dispenses matrix
3. [Auto] GoFaGo transfers to heater-shaker
4. [Auto] Mix 5 min @ 300 rpm
5. [Manual] UV exposure (30 s)
6. [Auto] Incubate 1 hr @ 37°C
7. [Auto] GoFaGo transfers to Tecan
8. [Auto] Measure turbidity @ 600 nm
9. [Auto] Export AnIML + CSV
```

### 4.5 Data Analysis

**Metriche da calcolare:**
- Turbidity vs polymer concentration
- Turbidity vs crosslinker ratio
- Replicate CV (target < 10%)
- Gelation success rate

### 4.6 Expected Outcomes

- Heatmap showing property variation across parameter space
- Correlation analysis between parameters and gelation
- Reproducibility < 10% CV

---

## 5. Fase 4: Usability Study (Settimana 4)

### 5.1 Recruitment

Recluta 8-12 partecipanti:
- 50% senza esperienza automazione
- 25% esperienza base
- 25% esperti (controllo)

### 5.2 Protocol

Per ogni partecipante (90 min totali):

1. **Intro (15 min):** Spiegazione sistema (no training hands-on)
2. **Task 1 (15 min):** Crea workflow semplice transfer+read
3. **Task 2 (10 min):** Modifica workflow esistente (aggiungi dilution)
4. **Task 3 (10 min):** Esegui workflow e esporta risultati
5. **SUS Questionnaire (10 min)**
6. **Interview (15 min):** Feedback qualitativo

### 5.3 Data Collection

**Quantitativo:**
- Task completion (yes/no)
- Time on task
- Error count
- SUS score

**Qualitativo:**
- Think-aloud notes
- Interview transcript
- Screen recording analysis

### 5.4 Analysis

- Calculate completion rates per task
- Calculate mean SUS score (target ≥ 70)
- Thematic analysis of qualitative feedback

---

## 6. Reporting dei Risultati

### Tabelle da preparare per la tesi:

1. **Navigation Accuracy Results**
   - Per-workstation statistics
   - Overall mean ± std

2. **Labware Transport Reliability**
   - Success rates per operation type
   - Failure mode analysis

3. **Workflow Execution Results**
   - Success rate per workflow type
   - Timing analysis

4. **Hydrogel Screening Results**
   - Heatmap figure
   - Reproducibility table

5. **Usability Results**
   - Task completion table
   - SUS score
   - Key qualitative findings

---

## 7. Troubleshooting Comune

### Robot non raggiunge posizione
- Verifica mappa aggiornata
- Check obstacle detection
- Riduci velocità

### Pick fallisce
- Calibra gripper
- Verifica ArUco visibility
- Adjust approach trajectory

### Workflow si blocca
- Check network connectivity
- Verify all servers running
- Check timeout settings

---

## 8. Timeline Suggerita

| Settimana | Attività |
|-----------|----------|
| 1 | Setup GoFaGo + Navigation tests |
| 2 | Transport tests + Integration tests |
| 3 | Hydrogel case study |
| 4 | Usability study + Data analysis |
| 5 | Writing and revision |

---

**Buon lavoro!**
