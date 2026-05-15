# Tecan M200 Pro - SiLA2 Server

Server SiLA2 per il controllo del plate reader **Tecan Infinite M200 Pro** in ambiente di laboratorio automatizzato.

## 📋 Caratteristiche

- ✅ **Protocollo SiLA2** - Standard per l'integrazione di strumenti di laboratorio
- ✅ **gRPC** - Comunicazione ad alte prestazioni
- ✅ **Output AnIML** - Standard ASTM E1947 per dati analitici
- ✅ **Configurazione flessibile** - Percorsi e opzioni configurabili via JSON
- ✅ **Compatibilità** - Basato sul bridge iControlBridge esistente

## 🚀 Quick Start

### 1. Compilazione

```powershell
cd TecanSiLA2Server
dotnet build
```

### 2. Configurazione

Modifica `appsettings.json` nella cartella `TecanSiLA2Server` (NON in bin/Debug):

```json
{
  "TecanSiLA2Server": {
    "GrpcPort": 50051,
    "ProtocolsPath": "C:\\Path\\To\\Your\\Protocols",
    "ResultsPath": "C:\\Path\\To\\Your\\Results",
    "DefaultConnectionString": "usb",
    "ConnectionTimeoutSeconds": 30
  }
}
```

### 3. Avvio Server

```powershell
.\bin\Debug\net48\TecanSiLA2Server.exe
```

### 4. Test con Client

```powershell
cd TestClient
dotnet build
.\bin\Debug\net48\TestClient.exe
```

---

## 📁 Struttura Progetto

```
TecanSiLA2Server/
├── appsettings.json          # ⭐ CONFIGURAZIONE (modifica questo!)
├── Program.cs                # Entry point
├── ServerConfiguration.cs    # Gestione configurazione
├── Features/
│   ├── PlateReaderService.sila.xml   # Definizione SiLA2
│   └── PlateReaderServiceImpl.cs     # Implementazione servizio
├── Instrument/
│   ├── TecanBridge.cs        # Bridge verso SDK Tecan
│   └── ResultParser.cs       # Parser risultati XML
├── AnIML/
│   └── TecanToAnIMLConverter.cs  # Conversione in formato AnIML
├── Protos/
│   └── PlateReaderService.proto  # Definizione gRPC
├── TestClient/               # Client di test interattivo
├── DiagnosticTool/           # Tool diagnostica connessione
└── lib/                      # DLL Tecan SDK
```

---

## ⚙️ Configurazione Completa

### Quale file modificare?

⭐ **Modifica sempre `TecanSiLA2Server/appsettings.json`** (nella cartella sorgente)

Il server cerca il file di configurazione in questo ordine:
1. Cartella di lavoro corrente
2. **Cartella sorgente del progetto** ← usa questo
3. Cartella dell'eseguibile (bin/Debug/net48)

### Parametri appsettings.json

| Parametro | Descrizione | Default |
|-----------|-------------|---------|
| `GrpcPort` | Porta del server gRPC | `50051` |
| `BindAddress` | Indirizzo di bind | `0.0.0.0` |
| `ProtocolsPath` | Cartella protocolli .mdfx | `Protocols` |
| `ResultsPath` | Cartella output risultati | `Results` |
| `DefaultConnectionString` | Connessione strumento | `usb` |
| `AutoConnectOnStartup` | Connetti automaticamente | `false` |
| `ConnectionTimeoutSeconds` | Timeout connessione (sec) | `30` |
| `GenerateExcel` | Genera file Excel | `true` |
| `GenerateAnIML` | Genera file AnIML | `true` |
| `GenerateCsv` | Genera file CSV | `false` |

### Stringhe di Connessione

| Valore | Descrizione |
|--------|-------------|
| `usb` | Solo strumenti USB reali (consigliato) |
| `sim` | Solo simulatori (per test) |
| `usb_auto` o `last` | Auto-connetti all'ultimo strumento |
| `any` | USB + Simulatori |

### Formato Percorsi

I percorsi possono essere:
- **Assoluti**: `C:\\Users\\user\\Desktop\\Protocols`
- **Relativi**: `Protocols` (relativo alla cartella del config)

⚠️ **Importante per JSON**:
- I backslash devono essere raddoppiati: `C:\\Path\\To\\Folder`
- Oppure usa forward slash: `C:/Path/To/Folder`

---

## 🔌 API SiLA2

### Proprietà (Observable)

| Proprietà | Tipo | Descrizione |
|-----------|------|-------------|
| `IsConnected` | bool | Stato connessione |
| `OperationalStatus` | string | Stato operativo (Idle, Busy, Error) |
| `CurrentTemperature` | double | Temperatura piastra (°C) |
| `InstrumentInfo` | struct | Info strumento (SN, modello, firmware) |

### Comandi

| Comando | Parametri | Descrizione |
|---------|-----------|-------------|
| `Connect` | connectionString | Connetti allo strumento |
| `Disconnect` | - | Disconnetti |
| `PlateIn` | - | Inserisci piastra nel lettore |
| `PlateOut` | - | Espelli piastra |
| `SetTemperature` | temperature (°C) | Imposta temperatura (4-45°C) |
| `TurnOffTemperature` | - | Spegni controllo temperatura |
| `RunMeasurement` | protocol, plateId, sampleSetId | Esegui protocollo di misura |
| `ListProtocols` | - | Lista protocolli .mdfx disponibili |
| `GetAnIMLResult` | plateId | Ottieni risultato in formato AnIML |

---

## 🧪 Test Client

### Comandi Interattivi

```
help       - Mostra aiuto
status     - Stato strumento e info
connect    - Connetti (premi Enter per USB, scrivi 'sim' per simulatore)
disconnect - Disconnetti
platein    - Muovi piastra dentro
plateout   - Muovi piastra fuori
temp       - Imposta temperatura
tempoff    - Spegni temperatura
protocols  - Lista protocolli disponibili
run        - Esegui un protocollo
result     - Ottieni risultato AnIML
exit       - Esci
```

### Esempio Sessione

```
Tecan> connect
Connection string (press Enter for USB only, 'sim' for simulator):
Connecting with: usb
Result: SUCCESS

Tecan> status
Instrument Status:
  Connected:    True
  Status:       Idle
  Temperature:  25.0 °C
  Product:      infinite 200Pro
  Serial:       2511004584

Tecan> protocols
Found 3 protocols:
  1. Absorbance_260nm.mdfx
  2. Fluorescence_FITC.mdfx
  3. Luminescence.mdfx

Tecan> run
Protocol file name: Absorbance_260nm.mdfx
Plate ID: PLATE001
Sample Set ID: SET001
Starting measurement...
Result: SUCCESS
```

---

## 📊 Formati Output

### XML (Tecan nativo)
- Sempre generato
- Contiene tutti i dati raw della misura
- Path: `Results/XML/`

### Excel (.xls)
- Per lettura operatori umani
- Richiede Microsoft Excel installato
- Path: `Results/Excel/`

### AnIML (ASTM E1947)
- Standard internazionale per dati analitici
- Interoperabile con altri sistemi LIMS
- Path: `Results/AnIML/`

Struttura AnIML:
```xml
<AnIML version="1.0">
  <SampleSet>
    <Sample sampleID="PLATE001">
      <Result name="Absorbance260">
        <SeriesSet>
          <Series name="Well" seriesType="String">
            <values>A1 A2 A3...</values>
          </Series>
          <Series name="Value" seriesType="Float64">
            <values>0.523 0.612 0.489...</values>
          </Series>
        </SeriesSet>
      </Result>
    </Sample>
  </SampleSet>
</AnIML>
```

---

## 🔧 Troubleshooting

### "Failed to connect - no USB instrument found"

1. ✅ Verifica che il Tecan M200 Pro sia **acceso**
2. ✅ Controlla il **cavo USB**
3. ✅ Apri **Gestione Dispositivi** e cerca "Tecan" sotto dispositivi USB
4. ✅ Se non presente, reinstalla i driver da:
   `C:\Program Files\Tecan\iControl\Drivers`

### "Connection timed out"

Il timeout di default è 30 secondi. Se serve più tempo:
```json
"ConnectionTimeoutSeconds": 60
```

### Finestra selezione strumento vuota

- Con `usb`: nessuno strumento USB fisico trovato
- Prova `sim` per testare con un simulatore

### Errore "Invalid JSON"

- Il file deve iniziare con `{` e finire con `}`
- I backslash vanno raddoppiati: `C:\\Users\\...`
- I commenti `//` sono supportati

### "gRPC error: failed to connect to all addresses"

Il server non è in esecuzione o è crashato. Controlla la console del server.

---

## 🔒 Note Tecniche

### Thread STA
L'SDK Tecan usa componenti COM che richiedono un thread STA (Single-Threaded Apartment). Il server gestisce automaticamente questo requisito.

### Architettura x86
Il progetto è compilato per x86 perché l'SDK Tecan è a 32-bit.

### DLL Richieste
Le DLL Tecan nella cartella `lib/` includono:
- SDK base: `Tecan.At.*.dll`
- Simulatori: `Tecan.At.Communication.SIM.*.dll`
- Device: `Tecan.At.Instrument.Reader.*.dll`

---

## 📚 Dipendenze

- .NET Framework 4.8
- Tecan SDK (DLL in cartella `lib/`)
- Grpc.Core 2.46.6
- Google.Protobuf 3.25.2
- Microsoft.Extensions.Hosting 8.0.1

---

## 📄 Licenza

Uso interno - ChemLab / Università di Milano-Bicocca

---

## 📞 Supporto

Per problemi tecnici:
1. Controlla la sezione Troubleshooting
2. Verifica i log nella console del server
3. Usa il DiagnosticTool per test di connessione
