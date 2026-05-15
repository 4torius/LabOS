// TecanBridge.cs
// Bridge layer che incapsula la logica di comunicazione con il Tecan M200 Pro
// Riutilizza il codice esistente da iControlBridge

using System;
using System.Collections.Generic;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using System.Xml.Linq;
using Microsoft.Extensions.Logging;

// Tecan SDK imports - allineati al bridge originale
using Tecan.At.Common.DocumentManagement;
using Tecan.At.Common.DocumentManagement.Reader;
using Tecan.At.Common.FileManagement;
using Tecan.At.Common.Results;
using Tecan.At.Instrument.Common;
using Tecan.At.Instrument.Common.Reader;
using Tecan.At.Measurement;
using Tecan.At.Measurement.Server;
using Tecan.At.XFluor.Connect;
using Tecan.At.XFluor.Core;
using Tecan.At.XFluor.ExcelOutput;

namespace TecanSiLA2Server.Instrument
{
    /// <summary>
    /// Bridge per la comunicazione con il Tecan M200 Pro.
    /// Incapsula tutta la logica di interazione con l'SDK Tecan.
    /// </summary>
    public class TecanBridge : IDisposable
    {
        private readonly ILogger<TecanBridge> _logger;
        private readonly ServerConfiguration _config;
        private MeasurementServer? _server;
        private bool _isConnected;
        private readonly object _lock = new();
        private string _currentStatus = "Disconnected";
        private string _lastMessage = "Not connected";

        // Paths
        private readonly string _rootPath;
        private readonly string _protocolsPath;
        private readonly string _resultsPath;

        public TecanBridge(ILogger<TecanBridge> logger, ServerConfiguration config)
        {
            _logger = logger;
            _config = config;
            _rootPath = Path.GetDirectoryName(config.ProtocolsPath) ?? AppDomain.CurrentDomain.BaseDirectory;
            _protocolsPath = config.ProtocolsPath;
            _resultsPath = config.ResultsPath;

            // Ensure directories exist
            Directory.CreateDirectory(_protocolsPath);
            Directory.CreateDirectory(Path.Combine(_resultsPath, "XML"));
            Directory.CreateDirectory(Path.Combine(_resultsPath, "CSV"));
            Directory.CreateDirectory(Path.Combine(_resultsPath, "AnIML"));
            Directory.CreateDirectory(Path.Combine(_resultsPath, "Excel"));

            // Initialize Tecan SDK
            try
            {
                ObjectFactory.AddEntryPoint(new DocumentEntryPoint());
                try
                {
                    XFluorSettings.ResultPresentationSettings.PolarShowPolarization = true;
                    XFluorSettings.ResultPresentationSettings.PolarShowAnisotropy = true;
                }
                catch { /* Ignorable */ }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Failed to initialize Tecan SDK");
                throw;
            }
        }

        #region Properties

        public bool IsConnected
        {
            get
            {
                lock (_lock) { return _isConnected; }
            }
        }

        public string CurrentStatus
        {
            get
            {
                lock (_lock) { return _currentStatus; }
            }
        }

        public string LastMessage
        {
            get
            {
                lock (_lock) { return _lastMessage; }
            }
        }

        public string ProtocolsPath => _protocolsPath;
        public string ResultsPath => _resultsPath;

        #endregion

        #region Connection Methods

        /// <summary>
        /// Ottiene informazioni base sullo strumento connesso (versione tuple)
        /// </summary>
        private (string ProductName, string SerialNumber, bool IsSimulated) GetInstrumentInfoTuple()
        {
            if (!_isConnected || _server == null)
                return ("Not Connected", "N/A", false);

            try
            {
                var info = _server.ConnectedReader.Information;
                return (info.GetProductName(), info.GetInstrumentSerial(), info.IsSimulated());
            }
            catch
            {
                return ("Error", "N/A", false);
            }
        }

        public bool Connect(string connectionString = "")
        {
            lock (_lock)
            {
                if (_isConnected)
                {
                    _logger.LogWarning("Already connected");
                    return true;
                }

                // L'SDK Tecan richiede un thread STA per la comunicazione COM
                // Eseguiamo la connessione in un thread STA dedicato
                Exception? connectionException = null;

                var staThread = new Thread(() =>
                {
                    try
                    {
                        ConnectInternal(connectionString);
                    }
                    catch (Exception ex)
                    {
                        connectionException = ex;
                    }
                });
                staThread.SetApartmentState(ApartmentState.STA);
                staThread.Start();
                
                // Timeout più lungo per permettere la selezione dello strumento via GUI
                int timeoutSeconds = _config.ConnectionTimeoutSeconds > 0 ? _config.ConnectionTimeoutSeconds : 60;
                staThread.Join(TimeSpan.FromSeconds(timeoutSeconds));

                if (connectionException != null)
                {
                    UpdateStatus("Error", connectionException.Message);
                    _logger.LogError(connectionException, "Exception during connection");
                    return false;
                }

                // Usa _isConnected invece di una variabile locale - è settato da ConnectInternal
                return _isConnected;
            }
        }

        /// <summary>
        /// Logica interna di connessione - deve essere chiamata da un thread STA
        /// </summary>
        private bool ConnectInternal(string connectionString)
        {
            try
            {
                _server = new MeasurementServer();
                
                // Determina la stringa di connessione
                // Il SDK Tecan supporta: USB, RS232, SIM (simulatori)
                // porttype: USB|SIM|RS232|IP  
                // type: READER, STACKER
                // option: default (usa ultimo strumento), dialog (forza selezione)
                string connStr;
                
                var connLower = connectionString?.ToLower() ?? "";
                
                if (string.IsNullOrEmpty(connectionString) || connLower == "default" || connLower == "usb")
                {
                    // DEFAULT: Solo USB per strumenti reali - NO simulatori
                    // IMPORTANTE: Usa la stessa stringa del bridge originale!
                    connStr = "porttype=USB, type=READER";
                    _logger.LogInformation("Using USB-only connection (real instruments)");
                }
                else if (connLower == "sim" || connLower == "simulator")
                {
                    // Solo simulatori
                    connStr = "porttype=SIM, type=READER";
                    _logger.LogInformation("Using Simulator connection");
                }
                else if (connLower == "usb_auto" || connLower == "lastconnected" || connLower == "last")
                {
                    // Usa l'ultimo strumento USB connesso (auto-connect)
                    connStr = "porttype=USB, type=READER, option=default";
                    _logger.LogInformation("Using last connected USB instrument");
                }
                else if (connLower == "dialog" || connLower == "usb_dialog" || connLower == "select")
                {
                    // Forza la finestra di selezione strumenti
                    connStr = "porttype=USB, type=READER, option=dialog";
                    _logger.LogInformation("Using USB connection with instrument selection dialog");
                }
                else if (connLower == "any" || connLower == "all")
                {
                    // USB + Simulatori (vecchio comportamento)
                    connStr = "porttype=USB|SIM, type=READER";
                    _logger.LogWarning("Using USB+SIM connection - may connect to simulator!");
                }
                else
                {
                    // Usa stringa di connessione personalizzata
                    connStr = connectionString ?? "";
                    _logger.LogInformation("Using custom connection string");
                }
                
                _logger.LogInformation("Connecting with string: {ConnStr}", connStr);
                
                // LastConnection tenta di connettersi con la configurazione specificata
                // e mostra la finestra di selezione se più strumenti sono disponibili
                if (_server.Connect(InstrumentConnectionMethod.LastConnection, connStr))
                {
                    // Verifica lo strumento connesso
                    var info = _server.ConnectedReader.Information;
                    bool isSimulated = info.IsSimulated();
                    string productName = info.GetProductName();
                    string serial = info.GetInstrumentSerial();
                    
                    _logger.LogInformation("Connected to: {ProductName} (SN: {Serial})", productName, serial);
                    
                    if (isSimulated)
                    {
                        _logger.LogWarning("WARNING: Connected to SIMULATOR, not real hardware!");
                    }
                    
                    _isConnected = true;
                    UpdateStatus("Idle", $"Connected to {productName} ({(isSimulated ? "SIMULATED" : serial)})");
                    return true;
                }
                else
                {
                    UpdateStatus("Error", "Connection failed - no instrument found");
                    _logger.LogError("Failed to connect - no USB instrument found. Check that:");
                    _logger.LogError("  1. Tecan M200 Pro is powered ON");
                    _logger.LogError("  2. USB cable is connected");
                    _logger.LogError("  3. Tecan USB drivers are installed");
                    return false;
                }
            }
            catch (Exception ex)
            {
                UpdateStatus("Error", ex.Message);
                _logger.LogError(ex, "Exception during connection");
                return false;
            }
        }

        public bool Disconnect()
        {
            lock (_lock)
            {
                try
                {
                    _server?.Disconnect();
                    _isConnected = false;
                    UpdateStatus("Disconnected", "Disconnected from instrument");
                    _logger.LogInformation("Disconnected from Tecan instrument");
                    return true;
                }
                catch (Exception ex)
                {
                    _logger.LogError(ex, "Exception during disconnection");
                    return false;
                }
            }
        }

        #endregion

        #region Plate Movement

        public bool PlateIn()
        {
            if (!EnsureConnected()) return false;
            try
            {
                _server!.ConnectedReader.Movement.PlateIn();
                _logger.LogInformation("Plate moved in");
                return true;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "PlateIn failed");
                return false;
            }
        }

        public bool PlateOut()
        {
            if (!EnsureConnected()) return false;
            try
            {
                _server!.ConnectedReader.Movement.PlateOut();
                _logger.LogInformation("Plate moved out");
                return true;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "PlateOut failed");
                return false;
            }
        }

        #endregion

        #region Temperature Control

        public bool SetTemperature(double temperatureCelsius)
        {
            if (!EnsureConnected()) return false;
            try
            {
                int tempInTenths = (int)(temperatureCelsius * 10);
                _server!.ConnectedReader.Temperature.SetTemperatureTarget(TEMPERATURE.Plate, tempInTenths);
                _server.ConnectedReader.Temperature.TemperatureOn(TEMPERATURE.Plate);
                _logger.LogInformation("Temperature set to {Temp}°C", temperatureCelsius);
                return true;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "SetTemperature failed");
                return false;
            }
        }

        public bool TurnOffTemperature()
        {
            if (!EnsureConnected()) return false;
            try
            {
                _server!.ConnectedReader.Temperature.TemperatureOff(TEMPERATURE.Plate);
                _logger.LogInformation("Temperature control turned off");
                return true;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "TurnOffTemperature failed");
                return false;
            }
        }

        public double GetCurrentTemperature()
        {
            if (!EnsureConnected()) return double.NaN;
            try
            {
                int tempInTenths = _server!.ConnectedReader.Temperature.GetTemperatureCurrent(TEMPERATURE.Plate);
                return tempInTenths / 10.0;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "GetCurrentTemperature failed");
                return double.NaN;
            }
        }

        #endregion

        #region Instrument Info

        public InstrumentInfo GetInstrumentInfo()
        {
            var info = new InstrumentInfo();
            if (!EnsureConnected()) return info;

            try
            {
                info.SerialNumber = _server!.ConnectedReader.Information.GetInstrumentSerial();
                info.ProductName = _server.ConnectedReader.Information.GetProductName();
                info.IsSimulated = _server.ConnectedReader.Information.IsSimulated();

                var definitions = _server.ConnectedReader.Information.GetInstrumentDefinitions();
                if (definitions?.DocumentContent is TecanReaderDefinition readerDef)
                {
                    info.FirmwareVersion = "Available"; // Extract from definitions if needed
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "GetInstrumentInfo failed");
            }

            return info;
        }

        #endregion

        #region Measurement Execution

        /// <summary>
        /// Esegue una misurazione e ritorna il percorso del file XML dei risultati
        /// </summary>
        public async Task<MeasurementResult> RunMeasurementAsync(
            string protocolFile,
            string plateId,
            IProgress<MeasurementProgress>? progress = null,
            CancellationToken cancellationToken = default)
        {
            _logger.LogInformation("[TecanBridge] RunMeasurementAsync STARTED - Protocol='{Protocol}', PlateId='{PlateId}'", 
                protocolFile, plateId);
            
            var result = new MeasurementResult();

            if (!EnsureConnected())
            {
                _logger.LogWarning("[TecanBridge] Not connected - returning error");
                result.Success = false;
                result.ErrorMessage = "Not connected to instrument";
                return result;
            }

            lock (_lock)
            {
                if (_currentStatus == "Busy")
                {
                    _logger.LogWarning("[TecanBridge] Instrument is busy - returning error");
                    result.Success = false;
                    result.ErrorMessage = "Instrument is busy";
                    return result;
                }
            }

            // Resolve protocol path - try multiple variations
            string fullPath = Path.Combine(_protocolsPath, protocolFile);
            
            // Try different path combinations
            var pathsToTry = new[]
            {
                fullPath,
                fullPath + ".mdfx",  // Add extension if missing
                Path.Combine(_protocolsPath, protocolFile + ".mdfx"),
                protocolFile,
                protocolFile + ".mdfx"
            };
            
            string? resolvedPath = null;
            foreach (var path in pathsToTry)
            {
                if (File.Exists(path))
                {
                    resolvedPath = Path.GetFullPath(path);
                    _logger.LogInformation("[TecanBridge] Protocol resolved: '{Input}' -> '{Resolved}'", protocolFile, resolvedPath);
                    break;
                }
            }
            
            if (resolvedPath == null)
            {
                _logger.LogError("[TecanBridge] Protocol not found. Tried: {Paths}", string.Join(", ", pathsToTry));
                result.Success = false;
                result.ErrorMessage = $"Protocol file not found: {protocolFile}. Available: {string.Join(", ", ListProtocols())}";
                return result;
            }
            
            fullPath = resolvedPath;

            // Generate output file names
            string timestamp = DateTime.Now.ToString("yyyyMMdd_HHmmss");
            string cleanPlateId = string.IsNullOrEmpty(plateId) ? "Unknown" : 
                plateId.Replace(" ", "_").Replace("/", "-");
            string baseName = $"{cleanPlateId}_{timestamp}";

            result.XmlFilePath = Path.Combine(_resultsPath, "XML", $"{baseName}.xml");
            result.CsvFilePath = Path.Combine(_resultsPath, "CSV", $"{baseName}.csv");
            result.ExcelFilePath = Path.Combine(_resultsPath, "Excel", $"{baseName}.xls");
            result.AnIMLFilePath = Path.Combine(_resultsPath, "AnIML", $"{baseName}.animl");
            result.PlateId = plateId;

            // Run measurement in STA thread (required by Tecan SDK)
            var tcs = new TaskCompletionSource<bool>();
            var measurementThread = new Thread(() =>
            {
                try
                {
                    RunMeasurementWorker(fullPath, result, progress);
                    tcs.SetResult(true);
                }
                catch (Exception ex)
                {
                    result.Success = false;
                    result.ErrorMessage = ex.Message;
                    tcs.SetException(ex);
                }
            });
            measurementThread.SetApartmentState(ApartmentState.STA);
            measurementThread.Start();

            await tcs.Task;
            return result;
        }

        private void RunMeasurementWorker(
            string protocolPath,
            MeasurementResult result,
            IProgress<MeasurementProgress>? progress)
        {
            UpdateStatus("Busy", "Starting measurement...");
            progress?.Report(new MeasurementProgress { Percentage = 0, Message = "Initializing..." });

            ResultOutput? output = null;
            Form? dummyForm = null;

            try
            {
                dummyForm = new Form();

                string fileContent = File.ReadAllText(protocolPath);
                var scriptToRun = Tecan.At.Common.FileManagement.FileHandling.LoadXml(fileContent) as TecanFile;
                
                if (scriptToRun == null)
                {
                    throw new Exception("Failed to load protocol file");
                }
                
                var defs = (TecanReaderDefinition)_server!.ConnectedReader.Information
                    .GetInstrumentDefinitions().DocumentContent;
                string devName = _server.ConnectedReader.Information.GetProductName();
                bool isSim = _server.ConnectedReader.Information.IsSimulated();

                _server.UseInprocMessagingService = true;
                Guid runGuid = Guid.NewGuid();

                // Setup Output
                output = new ResultOutput(scriptToRun, new Dictionary<string, string>(), defs, runGuid);
                var multiOutput = new MultipleOutput();

                // XML Output (native - sempre abilitato)
                multiOutput.OutputList.Add(new XmlOutput(result.XmlFilePath!));
                
                // Excel Output (se abilitato)
                if (_config.GenerateExcel)
                {
                    multiOutput.OutputList.Add(new ExcelOutput(dummyForm));
                }

                output.MeasurementDataOutput = multiOutput;
                output.DeviceName = devName;
                output.Simulation = isSim;
                output.Init();
                output.StartListening(_server.MessagingService);

                progress?.Report(new MeasurementProgress { Percentage = 10, Message = "Running measurement..." });

                // Execute measurement
                UpdateStatus("Busy", "Measurement in progress...");
                _server.ActionsAsObjects = scriptToRun;
                _server.NewRunState(runGuid);
                _server.Run(runGuid);

                progress?.Report(new MeasurementProgress { Percentage = 80, Message = "Saving results..." });
                
                // Salvataggio Excel via automazione COM (se abilitato)
                if (_config.GenerateExcel)
                {
                    Thread.Sleep(2000); // Attendi che Excel renderizzi i dati
                    SaveExcelViaAutomation(result.ExcelFilePath!);
                }

                // Wait for XML file to be written
                for (int i = 0; i < 10; i++)
                {
                    if (File.Exists(result.XmlFilePath) && new System.IO.FileInfo(result.XmlFilePath).Length > 0)
                        break;
                    Thread.Sleep(500);
                }

                // Determine measurement type from XML
                if (File.Exists(result.XmlFilePath))
                {
                    result.MeasurementType = DetermineMeasurementTypeFromXml(result.XmlFilePath!);
                    
                    // Generate CSV if enabled
                    if (_config.GenerateCsv)
                    {
                        try
                        {
                            string serial = _server!.ConnectedReader.Information.GetInstrumentSerial();
                            string temp = (_server.ConnectedReader.Temperature.GetTemperatureCurrent(TEMPERATURE.Plate) / 10.0)
                                .ToString("0.0", CultureInfo.InvariantCulture);
                            
                            var parser = new ResultParser(result.XmlFilePath!, serial, devName, temp);
                            if (parser.HasData())
                            {
                                File.WriteAllLines(result.CsvFilePath!, parser.GetResultsAsIcontrolCsv());
                                _logger.LogInformation("CSV file generated: {Path}", result.CsvFilePath);
                            }
                        }
                        catch (Exception ex)
                        {
                            _logger.LogWarning(ex, "Failed to generate CSV file");
                        }
                    }
                }

                result.Success = true;
                progress?.Report(new MeasurementProgress { Percentage = 100, Message = "Completed" });
                UpdateStatus("Idle", "Measurement completed");
            }
            catch (Exception ex)
            {
                result.Success = false;
                result.ErrorMessage = ex.Message;
                UpdateStatus("Error", ex.Message);
                _logger.LogError(ex, "Measurement failed");
            }
            finally
            {
                if (output != null)
                {
                    output.StopListening(false);
                    output.RemoveAllExceptionListeners();
                }
                dummyForm?.Dispose();
                GC.Collect();
                GC.WaitForPendingFinalizers();
            }
        }

        private string DetermineMeasurementTypeFromXml(string xmlPath)
        {
            try
            {
                var doc = System.Xml.Linq.XDocument.Load(xmlPath);
                var section = doc.Root?.Element("Section");
                var parameters = section?.Element("Parameters");
                var mode = parameters?.Elements("Parameter")
                    .FirstOrDefault(p => p.Attribute("Name")?.Value == "Mode")?
                    .Attribute("Value")?.Value ?? "";

                if (mode.IndexOf("Absorbance", StringComparison.OrdinalIgnoreCase) >= 0)
                    return "Absorbance";
                if (mode.IndexOf("Fluorescence", StringComparison.OrdinalIgnoreCase) >= 0)
                    return "Fluorescence";
                if (mode.IndexOf("Luminescence", StringComparison.OrdinalIgnoreCase) >= 0)
                    return "Luminescence";

                return "Mixed";
            }
            catch
            {
                return "Unknown";
            }
        }

        /// <summary>
        /// Salva il file Excel attivo tramite COM Automation
        /// </summary>
        private void SaveExcelViaAutomation(string savePath)
        {
            object? excelApp = null;
            object? activeWorkbook = null;

            try
            {
                _logger.LogInformation("Attempting to save Excel file: {Path}", savePath);

                // Ottieni l'istanza Excel attiva (aperta dalla libreria Tecan)
                try
                {
                    excelApp = Marshal.GetActiveObject("Excel.Application");
                }
                catch
                {
                    _logger.LogWarning("No active Excel instance found");
                    return;
                }

                if (excelApp != null)
                {
                    // Disabilita messaggi di conferma
                    excelApp.GetType().InvokeMember("DisplayAlerts", 
                        BindingFlags.SetProperty, null, excelApp, new object[] { false });

                    // Ottieni workbook attivo
                    activeWorkbook = excelApp.GetType().InvokeMember("ActiveWorkbook", 
                        BindingFlags.GetProperty, null, excelApp, null);

                    if (activeWorkbook != null)
                    {
                        // SaveAs con formato xlExcel8 (.xls) = 56
                        object[] args = new object[] { savePath, 56 };
                        activeWorkbook.GetType().InvokeMember("SaveAs", 
                            BindingFlags.InvokeMethod, null, activeWorkbook, args);

                        _logger.LogInformation("Excel file saved successfully");
                    }
                }
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "Error saving Excel file");
            }
            finally
            {
                // Chiudi Excel e rilascia risorse
                if (excelApp != null)
                {
                    try
                    {
                        excelApp.GetType().InvokeMember("Quit", 
                            BindingFlags.InvokeMethod, null, excelApp, null);
                        Marshal.ReleaseComObject(excelApp);
                    }
                    catch { }
                }

                if (activeWorkbook != null)
                {
                    try { Marshal.ReleaseComObject(activeWorkbook); } catch { }
                }
            }
        }

        #endregion

        #region Protocol Management

        public string[] ListProtocols()
        {
            try
            {
                return Directory.GetFiles(_protocolsPath, "*.mdfx")
                    .Select(Path.GetFileName)
                    .Where(f => f != null)
                    .ToArray()!;
            }
            catch (Exception ex)
            {
                _logger.LogError(ex, "ListProtocols failed");
                return Array.Empty<string>();
            }
        }

        #endregion

        #region Helper Methods

        private bool EnsureConnected()
        {
            lock (_lock)
            {
                if (!_isConnected || _server == null)
                {
                    _logger.LogWarning("Operation attempted while not connected");
                    return false;
                }
                return true;
            }
        }

        private void UpdateStatus(string status, string message)
        {
            lock (_lock)
            {
                _currentStatus = status;
                _lastMessage = message;
            }
            _logger.LogInformation("[{Status}] {Message}", status, message);
        }

        public void Dispose()
        {
            Disconnect();
            GC.SuppressFinalize(this);
        }

        #endregion
    }

    #region Data Classes

    public class InstrumentInfo
    {
        public string SerialNumber { get; set; } = "Unknown";
        public string ProductName { get; set; } = "Unknown";
        public string FirmwareVersion { get; set; } = "Unknown";
        public bool IsSimulated { get; set; } = false;
    }

    public class MeasurementResult
    {
        public bool Success { get; set; }
        public string? ErrorMessage { get; set; }
        public string? XmlFilePath { get; set; }
        public string? CsvFilePath { get; set; }
        public string? AnIMLFilePath { get; set; }
        public string? ExcelFilePath { get; set; }
        public string? PlateId { get; set; }
        public string MeasurementType { get; set; } = "Unknown";
    }

    public class MeasurementProgress
    {
        public int Percentage { get; set; }
        public string Message { get; set; } = "";
    }

    #endregion
}
