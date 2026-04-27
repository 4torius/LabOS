# Riepilogo Funzionalità - iControlBridge vs TecanSiLA2Server

## Confronto Funzionalità

| Funzionalità | iControlBridge (HTTP) | TecanSiLA2Server (gRPC/SiLA2) | Note |
|--------------|----------------------|------------------------------|------|
| **Connessione** |
| Connect | ✅ `/api/connect` | ✅ `Connect` | Supporta dialog, sim, usb, lastconnected |
| Disconnect | ✅ `/api/disconnect` | ✅ `Disconnect` | |
| **Movimentazione Piastra** |
| PlateIn | ✅ `/api/plate_in` | ✅ `PlateIn` | |
| PlateOut | ✅ `/api/plate_out` | ✅ `PlateOut` | |
| **Controllo Temperatura** |
| SetTemperature | ✅ `/api/set_temperature?temp=37` | ✅ `SetTemperature` | Range 4-45°C |
| TurnOffTemperature | ✅ `/api/set_temperature_off` | ✅ `TurnOffTemperature` | |
| GetTemperatureStatus | ✅ `/api/get_temperature_status` | ✅ `GetCurrentTemperature` | |
| **Misurazione** |
| RunAnalysis | ✅ `/api/run_analysis?file=...` | ✅ `RunMeasurement` | Asincrono con progresso |
| Status | ✅ `/api/status` | ✅ `GetOperationalStatus` | Observable in SiLA2 |
| **Output Risultati** |
| XML Output | ✅ Sempre | ✅ Sempre | Formato nativo Tecan |
| CSV Output | ✅ Sempre | ✅ Configurabile | Formato i-control compatibile |
| Excel Output | ✅ Sempre | ⚙️ Configurabile (disabilitato) | Richiede Excel installato |
| AnIML Output | ❌ Non disponibile | ✅ Configurabile | Standard ASTM E1947 |
| **Info Strumento** |
| InstrumentInfo | ❌ Limitato | ✅ Completo | Serial, Name, Simulated |
| IsSimulated | ✅ Interno | ✅ Esposto | |
| **Protocolli** |
| ListProtocols | ❌ Non disponibile | ✅ `ListProtocols` | Lista file .mdfx |

## Nuove Funzionalità in TecanSiLA2Server

1. **SiLA2 Compliance**: Protocollo standard per laboratori robotizzati
2. **AnIML Output**: Formato standard ASTM per interoperabilità
3. **Observable Properties**: Sottoscrizione real-time a stato, temperatura, etc.
4. **File di Configurazione**: `appsettings.json` per personalizzare il server
5. **gRPC**: Comunicazione binaria efficiente vs HTTP/REST

## File di Configurazione (appsettings.json)

```json
{
  "TecanSiLA2Server": {
    "GrpcPort": 50051,
    "BindAddress": "0.0.0.0",
    "ProtocolsPath": "",
    "ResultsPath": "",
    "DefaultConnectionString": "",
    "AutoConnectOnStartup": false,
    "GenerateCsv": true,
    "GenerateExcel": false,
    "GenerateAnIML": true,
    "ConnectionTimeoutSeconds": 30,
    "MeasurementTimeoutSeconds": 0,
    "ShowPolarization": true,
    "ShowAnisotropy": true
  }
}
```

### Opzioni di Connessione

| Valore | Descrizione |
|--------|-------------|
| `""` o `"default"` | USB + Simulatori, mostra dialog selezione |
| `"sim"` | Solo simulatori |
| `"usb"` | Solo strumenti USB reali |
| `"lastconnected"` | Usa ultimo strumento connesso |
| Custom string | Es. `"porttype=USB, type=READER, option=M200"` |

## Struttura Cartelle

```
TecanSiLA2Server/
├── bin/Debug/net48/
│   ├── TecanSiLA2Server.exe
│   ├── appsettings.json
│   ├── Protocols/              # File .mdfx
│   │   └── *.mdfx
│   └── Results/
│       ├── XML/                # Output XML nativo
│       ├── CSV/                # Output CSV formato i-control
│       ├── Excel/              # Output Excel (se abilitato)
│       └── AnIML/              # Output AnIML standard
└── lib/                        # DLL Tecan SDK
```

## Comandi TestClient

```
help          - Mostra aiuto
status        - Stato corrente
info          - Info strumento
connect       - Connetti (dialog)
connect sim   - Connetti simulatore
connect usb   - Connetti USB
disconnect    - Disconnetti
platein       - Piastra dentro
plateout      - Piastra fuori
temp [°C]     - Imposta temperatura
tempoff       - Spegni temperatura
protocols     - Lista protocolli
run [file]    - Esegui protocollo
exit          - Esci
```
