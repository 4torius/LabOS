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

        private bool _isConnecting;

        private readonly object _lock = new();

        private string _currentStatus = "Disconnected";

        private string _lastMessage = "Not connected";



        // Paths

        private readonly string _rootPath;

        private readonly string _protocolsPath;

        private readonly string _resultsPath;



        // PlateType override lookup (see PlateTypeLibrary.cs) -- empty

        // dictionary if PlateTypesPath is unset or unreadable, in which case

        // RunMeasurement's PlateType parameter stays a documented no-op.

        private readonly Dictionary<string, PlateTypeDefinition> _plateTypes;



        public TecanBridge(ILogger<TecanBridge> logger, ServerConfiguration config)

        {

            _logger = logger;

            _config = config;

            _rootPath = Path.GetDirectoryName(config.ProtocolsPath) ?? AppDomain.CurrentDomain.BaseDirectory;

            _protocolsPath = config.ProtocolsPath;

            _resultsPath = config.ResultsPath;

            _plateTypes = PlateTypeLibrary.Load(config.PlateTypesPath, _logger);



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

            // The lock is only held for this initial check-and-set, NOT across

            // the STA thread's Join below. ConnectInternal/UpdateStatus (running

            // on the STA thread) need the same _lock to report status — holding

            // it here for the whole Join would deadlock that thread until the

            // timeout expired, making every Connect() call (even successful

            // ones) block for the full configured timeout instead of returning

            // as soon as the instrument responds. _isConnecting takes over the

            // job of preventing two concurrent Connect() calls.

            lock (_lock)

            {

                if (_isConnecting)

                {

                    _logger.LogWarning("Connection already in progress");

                    return false;

                }

                _isConnecting = true;

            }



            try

            {

                // _isConnected only reflects our own last successful ConnectInternal()

                // (or an explicit Disconnect()) — nothing else ever revalidates it

                // against the real COM/hardware state in between. If the instrument

                // drops off silently (USB unplugged, power-cycled, driver crash),

                // the flag stays stuck on true and this method would previously

                // short-circuit "already connected" without ever touching the SDK

                // again. Always disconnect first so every Connect() genuinely

                // re-validates against the instrument instead of trusting the flag.

                // Disconnect() re-enters _lock, which is safe: C# lock/Monitor is

                // reentrant for the owning thread.

                Disconnect();



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

                return IsConnected;

            }

            finally

            {

                lock (_lock) { _isConnecting = false; }

            }

        }



        /// <summary>

        /// Logica interna di connessione - deve essere chiamata da un thread STA

        /// </summary>

        /// <summary>
        /// One connection attempt. True only when an instrument was actually
        /// reached and identified.
        /// </summary>
        /// <remarks>
        /// A fresh MeasurementServer per attempt: after a failed Connect the
        /// SDK's server object is not in a state worth retrying through, and
        /// reusing it makes the second attempt fail for reasons that have
        /// nothing to do with the instrument.
        /// </remarks>
        private bool TryConnect(InstrumentConnectionMethod method, string connStr)
        {
            _server = new MeasurementServer();

            if (!_server.Connect(method, connStr))
            {
                _logger.LogInformation("{Method} found no instrument", method);
                return false;
            }

            var info = _server.ConnectedReader.Information;
            bool isSimulated = info.IsSimulated();
            string productName = info.GetProductName();
            string serial = info.GetInstrumentSerial();

            _logger.LogInformation("Connected to: {ProductName} (SN: {Serial}) via {Method}",
                productName, serial, method);

            if (isSimulated)
            {
                _logger.LogWarning("WARNING: Connected to SIMULATOR, not real hardware!");
            }

            _isConnected = true;
            UpdateStatus("Idle", $"Connected to {productName} ({(isSimulated ? "SIMULATED" : serial)})");
            return true;
        }

        /// <summary>
        /// How one of the supported keywords should actually be connected.
        /// </summary>
        private struct ConnectionPlan
        {
            public InstrumentConnectionMethod Method;
            public string ConnString;
            /// <summary>Retry with a fresh enumeration if Method finds nothing.</summary>
            public bool AllowEnumerationFallback;
            public string Explanation;
        }

        /// <summary>
        /// Map a keyword onto a real SDK connection method and string.
        /// </summary>
        /// <remarks>
        /// These keywords used to differ only in the connection *string* while
        /// every one of them was passed to InstrumentConnectionMethod.LastConnection.
        /// So "dialog" and "last" were the same strategy with a different token
        /// appended, and the method that decides the whole approach never
        /// varied. Each keyword now selects the method that actually
        /// implements it.
        ///
        /// The methods the SDK exposes (Tecan.At.Instrument.Common):
        ///   LastConnection            replay the stored connection - the port
        ///                             the reader was on last time. Fast, and
        ///                             wrong as soon as it is moved.
        ///   Manually                  connect from the given connection string,
        ///                             enumerating rather than replaying.
        ///   UserDialog                show the instrument-selection window.
        ///   UserDialogMinimalConnect  the same, minimal handshake.
        ///   EVOware / EVOlyzer / Magellan   connect as those applications do.
        /// </remarks>
        private ConnectionPlan ResolveConnection(string connectionString)
        {
            var key = connectionString?.Trim().ToLower() ?? "";

            if (key == "" || key == "default" || key == "usb")
            {
                // The everyday path: try the stored connection first because it
                // is quick, and fall back to enumerating when the reader has
                // been moved to a different USB port.
                return new ConnectionPlan
                {
                    Method = InstrumentConnectionMethod.LastConnection,
                    ConnString = "porttype=USB, type=READER",
                    AllowEnumerationFallback = true,
                    Explanation = "stored connection, falling back to a fresh USB enumeration",
                };
            }

            if (key == "last" || key == "usb_auto" || key == "lastconnected")
            {
                // An explicit request for the stored connection: no fallback,
                // because the caller asked for this specific behaviour.
                return new ConnectionPlan
                {
                    Method = InstrumentConnectionMethod.LastConnection,
                    ConnString = "porttype=USB, type=READER",
                    AllowEnumerationFallback = false,
                    Explanation = "the stored connection only",
                };
            }

            if (key == "dialog" || key == "usb_dialog" || key == "select")
            {
                // Opens a window on the machine running the bridge, so it needs
                // somebody in front of it. It is the manual recovery path: the
                // selection is recorded as the new stored connection.
                return new ConnectionPlan
                {
                    Method = InstrumentConnectionMethod.UserDialog,
                    ConnString = "porttype=USB, type=READER",
                    AllowEnumerationFallback = false,
                    Explanation = "the instrument-selection dialog (needs someone at the machine)",
                };
            }

            if (key == "sim" || key == "simulator")
            {
                // A simulator has no meaningful stored connection to replay.
                return new ConnectionPlan
                {
                    Method = InstrumentConnectionMethod.Manually,
                    ConnString = "porttype=SIM, type=READER",
                    AllowEnumerationFallback = false,
                    Explanation = "the Tecan simulator",
                };
            }

            if (key == "any" || key == "all")
            {
                return new ConnectionPlan
                {
                    Method = InstrumentConnectionMethod.Manually,
                    ConnString = "porttype=USB|SIM, type=READER",
                    AllowEnumerationFallback = false,
                    Explanation = "USB or simulator, whichever answers first",
                };
            }

            // A raw SDK connection string, passed through untouched.
            return new ConnectionPlan
            {
                Method = InstrumentConnectionMethod.LastConnection,
                ConnString = connectionString ?? "",
                AllowEnumerationFallback = false,
                Explanation = "a custom connection string",
            };
        }

        private bool ConnectInternal(string connectionString)
        {
            try
            {
                var plan = ResolveConnection(connectionString);
                _logger.LogInformation(
                    "Connecting via {Method} using '{ConnStr}' -- {Explanation}",
                    plan.Method, plan.ConnString, plan.Explanation);

                if (TryConnect(plan.Method, plan.ConnString))
                {
                    return true;
                }

                // LastConnection replays the port the reader was on the last
                // time something connected to it. Windows creates a separate
                // device instance per physical port, so moving the instrument
                // invalidates that record: the reader is present and powered,
                // iControl still connects to it, and this does not. Manually
                // connects from the string instead, and finds it wherever it is.
                //
                // Only ever a fallback, so a working setup takes the same path
                // it always did, and the case this rescues previously just
                // returned an error.
                if (plan.AllowEnumerationFallback)
                {
                    _logger.LogWarning(
                        "The stored connection matched no instrument. Retrying with a fresh " +
                        "enumeration -- this is what recovers a reader that has been moved to " +
                        "a different USB port.");

                    if (TryConnect(InstrumentConnectionMethod.Manually, plan.ConnString))
                    {
                        _logger.LogInformation(
                            "Connected after a fresh enumeration: the reader is on a different " +
                            "USB port than the stored connection expected.");
                        return true;
                    }
                }

                UpdateStatus("Error", "Connection failed - no instrument found");
                _logger.LogError("Failed to connect - no reader found via {Method}. Check that:", plan.Method);
                _logger.LogError("  1. Tecan M200 Pro is powered ON");
                _logger.LogError("  2. USB cable is connected");
                _logger.LogError("  3. iControl/Magellan is closed - they hold the USB connection exclusively");
                _logger.LogError("  4. Tecan USB drivers are installed");
                return false;
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

            CancellationToken cancellationToken = default,

            string? plateType = null)

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

                // Set Busy atomically with the check above — otherwise two

                // concurrent RunMeasurementAsync calls could both pass this

                // check before either sets status to Busy (UpdateStatus only

                // runs later, on the STA thread), starting two measurement

                // threads against the same physical instrument at once.

                _currentStatus = "Busy";

                _lastMessage = "Starting measurement...";

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

                    RunMeasurementWorker(fullPath, result, progress, plateType);

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



        /// <summary>

        /// Riscrive gli attributi file/range di ogni &lt;CyclePlate&gt;/&lt;PlateRange&gt;

        /// nel contenuto XML di un .mdfx, per usare una piastra diversa da

        /// quella con cui il protocollo e' stato creato nel Reader Editor.

        /// I due attributi vengono sempre cambiati insieme (sono accoppiati:

        /// il range dipende dalla piastra). Se il .mdfx non contiene nessun

        /// nodo CyclePlate/PlateRange (formato inatteso), lancia un'eccezione

        /// invece di restituire il file invariato senza avvisare nessuno.

        /// </summary>

        private static string RewritePlateType(string xmlContent, PlateTypeDefinition plateDef)

        {

            XNamespace ns = "tecan.at.schema.documents";

            var doc = XDocument.Parse(xmlContent);



            var cyclePlates = doc.Descendants(ns + "CyclePlate").ToList();

            var plateRanges = doc.Descendants(ns + "PlateRange").ToList();

            if (cyclePlates.Count == 0 || plateRanges.Count == 0)

            {

                throw new Exception(

                    "Impossibile applicare PlateType: nessun nodo <CyclePlate>/<PlateRange> trovato nel file .mdfx (formato inatteso).");

            }



            foreach (var cp in cyclePlates)

                cp.SetAttributeValue("file", plateDef.File);

            foreach (var pr in plateRanges)

                pr.SetAttributeValue("range", plateDef.Range);



            return doc.Declaration != null

                ? doc.Declaration + Environment.NewLine + doc.ToString(SaveOptions.DisableFormatting)

                : doc.ToString(SaveOptions.DisableFormatting);

        }



        private void RunMeasurementWorker(

            string protocolPath,

            MeasurementResult result,

            IProgress<MeasurementProgress>? progress,

            string? plateType = null)

        {

            UpdateStatus("Busy", "Starting measurement...");

            progress?.Report(new MeasurementProgress { Percentage = 0, Message = "Initializing..." });



            ResultOutput? output = null;

            Form? dummyForm = null;



            try

            {

                dummyForm = new Form();



                string fileContent = File.ReadAllText(protocolPath);

                if (!string.IsNullOrEmpty(plateType))

                {

                    // Explicit request to override the .mdfx's hardcoded

                    // plate -- fail loudly if we don't have a definition for

                    // it rather than silently keeping the .mdfx default,

                    // which would look successful while measuring the wrong

                    // geometry.

                    string plateTypeKey = plateType!;

                    if (!_plateTypes.TryGetValue(plateTypeKey, out var plateDef))

                    {

                        var known = _plateTypes.Count > 0 ? string.Join(", ", _plateTypes.Keys) : "(nessuna)";

                        throw new Exception(

                            $"PlateType '{plateTypeKey}' non trovato in PlateTypes.json. Piastre note: {known}. " +

                            "Lascia PlateType vuoto per usare il default del protocollo.");

                    }

                    fileContent = RewritePlateType(fileContent, plateDef);

                    _logger.LogInformation(

                        "[TecanBridge] PlateType override applicato: '{PlateType}' -> file={File}, range={Range}",

                        plateTypeKey, plateDef.File, plateDef.Range);

                }

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

