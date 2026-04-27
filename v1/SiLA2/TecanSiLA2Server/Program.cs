// Program.cs - TecanSiLA2Server Entry Point
// SiLA2 Server per Tecan M200 Pro Plate Reader

using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using Grpc.Core;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using TecanSiLA2Server.Features;
using TecanSiLA2Server.Instrument;
using TecanSiLA2Server.Services;

namespace TecanSiLA2Server
{
    public class Program
    {
        // SiLA2 Default port
        private const int DefaultPort = 50051;

        /// <summary>
        /// Entry point - STAThread è CRITICO per l'SDK Tecan che usa COM
        /// </summary>
        [STAThread]
        public static void Main(string[] args)
        {
            // Avvia il server async mantenendo il thread STA
            MainAsync(args).GetAwaiter().GetResult();
        }

        private static async Task MainAsync(string[] args)
        {
            Console.WriteLine("═══════════════════════════════════════════════════════════════════");
            Console.WriteLine("       Tecan M200 Pro - SiLA2 Server v1.0                          ");
            Console.WriteLine("       SiLA2 + AnIML Integration                                    ");
            Console.WriteLine("═══════════════════════════════════════════════════════════════════");
            Console.WriteLine();

            // La directory base è dove si trova l'eseguibile
            string exePath = AppDomain.CurrentDomain.BaseDirectory;
            Console.WriteLine($"Executable Path: {exePath}");
            
            // Cerca appsettings.json in più posizioni (in ordine di priorità):
            // 1. Cartella corrente di lavoro
            // 2. Cartella sorgente del progetto (3 livelli su da bin/Debug/net48)
            // 3. Cartella dell'eseguibile
            string configPath = FindConfigFile(exePath);
            var config = ServerConfiguration.Load(configPath);
            
            // Risolvi i percorsi relativi usando la directory dell'eseguibile come base
            // (o la cartella del config se è un percorso assoluto)
            string basePath = Path.GetDirectoryName(configPath) ?? exePath;
            config.ResolvePaths(basePath);
            
            Console.WriteLine($"Config: {configPath}");

            // Parse port from command line or use config
            int port = config.GrpcPort;
            if (args.Length > 0 && int.TryParse(args[0], out var parsedPort))
            {
                port = parsedPort;
            }

            try
            {
                var host = CreateHostBuilder(args, config, port).Build();

                var logger = host.Services.GetRequiredService<ILogger<Program>>();
                logger.LogInformation("Starting SiLA2 Server on port {Port}", port);

                PrintServerInfo(port, config);

                await host.RunAsync();
            }
            catch (Exception ex)
            {
                Console.ForegroundColor = ConsoleColor.Red;
                Console.WriteLine($"[FATAL ERROR] {ex.Message}");
                Console.ResetColor();
                Console.WriteLine("\nPress Enter to exit...");
                Console.ReadLine();
            }
        }

        private static IHostBuilder CreateHostBuilder(string[] args, ServerConfiguration config, int port)
        {
            return Host.CreateDefaultBuilder(args)
                .ConfigureLogging(logging =>
                {
                    logging.ClearProviders();
                    logging.AddConsole();
                    logging.SetMinimumLevel(LogLevel.Information);
                })
                .ConfigureServices((context, services) =>
                {
                    // Register configuration
                    services.AddSingleton(config);
                    
                    // Register TecanBridge as Singleton
                    services.AddSingleton<TecanBridge>(sp =>
                    {
                        var logger = sp.GetRequiredService<ILogger<TecanBridge>>();
                        return new TecanBridge(logger, config);
                    });

                    // Register SiLA2 Service Implementation
                    services.AddSingleton<PlateReaderServiceImpl>();

                    // Register SiLA2Common Service for Plug & Play
                    services.AddSingleton<SiLA2CommonServiceImpl>();

                    // Register gRPC Server as Hosted Service
                    services.AddHostedService<SiLA2ServerHostedService>(sp =>
                    {
                        var logger = sp.GetRequiredService<ILogger<SiLA2ServerHostedService>>();
                        var serviceImpl = sp.GetRequiredService<PlateReaderServiceImpl>();
                        var commonServiceImpl = sp.GetRequiredService<SiLA2CommonServiceImpl>();
                        return new SiLA2ServerHostedService(logger, serviceImpl, commonServiceImpl, port, config);
                    });
                });
        }

        /// <summary>
        /// Cerca appsettings.json in più posizioni
        /// </summary>
        private static string FindConfigFile(string exePath)
        {
            const string configName = "appsettings.json";
            
            // 1. Cartella corrente di lavoro
            string candidate = Path.Combine(Environment.CurrentDirectory, configName);
            if (File.Exists(candidate))
            {
                Console.WriteLine($"Found config in working directory");
                return candidate;
            }
            
            // 2. Cartella sorgente del progetto (risali da bin/Debug/net48)
            candidate = Path.GetFullPath(Path.Combine(exePath, @"..\..\..", configName));
            if (File.Exists(candidate))
            {
                Console.WriteLine($"Found config in project source directory");
                return candidate;
            }
            
            // 3. Cartella dell'eseguibile
            candidate = Path.Combine(exePath, configName);
            if (File.Exists(candidate))
            {
                Console.WriteLine($"Found config in executable directory");
                return candidate;
            }
            
            // Default: usa il percorso nella cartella exe (anche se non esiste)
            Console.WriteLine($"Config not found, using defaults");
            return Path.Combine(exePath, configName);
        }

        private static string FindRootPath(string startPath)
        {
            // Look for Protocols folder to identify root
            string candidate = Path.GetFullPath(Path.Combine(startPath, @"..\..\.."));
            if (Directory.Exists(Path.Combine(candidate, "Protocols")))
                return candidate;

            candidate = Path.GetFullPath(Path.Combine(startPath, @"..\.."));
            if (Directory.Exists(Path.Combine(candidate, "Protocols")))
                return candidate;

            candidate = Path.GetFullPath(Path.Combine(startPath, @".."));
            if (Directory.Exists(Path.Combine(candidate, "Protocols")))
                return candidate;

            if (Directory.Exists(Path.Combine(startPath, "Protocols")))
                return startPath;

            // Default: use exe path
            return startPath;
        }

        private static void PrintServerInfo(int port, ServerConfiguration config)
        {
            Console.ForegroundColor = ConsoleColor.Green;
            Console.WriteLine($"[OK] SiLA2 gRPC Server listening on port {port}");
            Console.ResetColor();
            Console.WriteLine();

            Console.ForegroundColor = ConsoleColor.Cyan;
            Console.WriteLine("═══════════════════════════════════════════════════════════════════");
            Console.WriteLine("  Feature: PlateReaderService");
            Console.WriteLine("  Category: instruments.platereader");
            Console.WriteLine("  Originator: it.chemlab");
            Console.WriteLine("═══════════════════════════════════════════════════════════════════");
            Console.ResetColor();
            Console.WriteLine();

            Console.WriteLine("Available Commands:");
            Console.WriteLine("  - Connect         : Connect to the Tecan instrument");
            Console.WriteLine("  - Disconnect      : Disconnect from the instrument");
            Console.WriteLine("  - PlateIn         : Move plate into the reader");
            Console.WriteLine("  - PlateOut        : Move plate out of the reader");
            Console.WriteLine("  - SetTemperature  : Set plate temperature (4-45°C)");
            Console.WriteLine("  - TurnOffTemperature : Turn off temperature control");
            Console.WriteLine("  - RunMeasurement  : Execute a measurement protocol");
            Console.WriteLine("  - ListProtocols   : List available .mdfx protocols");
            Console.WriteLine("  - GetAnIMLResult  : Retrieve results in AnIML format");
            Console.WriteLine();

            Console.WriteLine("Observable Properties:");
            Console.WriteLine("  - IsConnected       : Connection status");
            Console.WriteLine("  - OperationalStatus : Current operational state");
            Console.WriteLine("  - CurrentTemperature: Current plate temperature");
            Console.WriteLine("  - InstrumentInfo    : Instrument details");
            Console.WriteLine();

            Console.ForegroundColor = ConsoleColor.Yellow;
            Console.WriteLine("Directories:");
            Console.WriteLine($"  Protocols: {config.ProtocolsPath}");
            Console.WriteLine($"  Results:   {config.ResultsPath}");
            Console.WriteLine($"  AnIML:     {Path.Combine(config.ResultsPath, "AnIML")}");
            Console.ResetColor();
            Console.WriteLine();
            
            Console.WriteLine("Output Formats:");
            Console.WriteLine($"  XML:   Always enabled");
            Console.WriteLine($"  CSV:   {(config.GenerateCsv ? "Enabled" : "Disabled")}");
            Console.WriteLine($"  Excel: {(config.GenerateExcel ? "Enabled" : "Disabled")}");
            Console.WriteLine($"  AnIML: {(config.GenerateAnIML ? "Enabled" : "Disabled")}");
            Console.WriteLine();

            Console.WriteLine("SiLA Browser Connection:");
            Console.WriteLine($"  grpc://localhost:{port}");
            Console.WriteLine();

            Console.ForegroundColor = ConsoleColor.Gray;
            Console.WriteLine("Press Ctrl+C to stop the server...");
            Console.ResetColor();
            Console.WriteLine();
        }
    }

    /// <summary>
    /// Hosted Service per gestire il ciclo di vita del server gRPC
    /// </summary>
    public class SiLA2ServerHostedService : IHostedService
    {
        private readonly ILogger<SiLA2ServerHostedService> _logger;
        private readonly PlateReaderServiceImpl _serviceImpl;
        private readonly SiLA2CommonServiceImpl _commonServiceImpl;
        private readonly int _port;
        private Server? _server;
        private MdnsService? _mdnsService;

        private readonly ServerConfiguration _config;

        public SiLA2ServerHostedService(
            ILogger<SiLA2ServerHostedService> logger,
            PlateReaderServiceImpl serviceImpl,
            SiLA2CommonServiceImpl commonServiceImpl,
            int port,
            ServerConfiguration config)
        {
            _logger = logger;
            _serviceImpl = serviceImpl;
            _commonServiceImpl = commonServiceImpl;
            _port = port;
            _config = config;
        }

        public async Task StartAsync(CancellationToken cancellationToken)
        {
            _server = new Server
            {
                Services = { 
                    PlateReaderService.BindService(_serviceImpl),
                    SiLA2ServerInfo.BindService(_commonServiceImpl)  // Plug & Play support
                },
                Ports = { new ServerPort("0.0.0.0", _port, ServerCredentials.Insecure) }
            };

            _server.Start();
            _logger.LogInformation("gRPC Server started on port {Port}", _port);
            _logger.LogInformation("SiLA2Common (Plug & Play) enabled");

            // Registra il server per la scoperta mDNS
            try
            {
                var mdnsLogger = Microsoft.Extensions.Logging.Abstractions.NullLoggerFactory.Instance
                    .CreateLogger<MdnsService>();
                // Build safe mDNS service name (no spaces, no special chars)
                var mdnsName = _config.ServerName.Replace(" ", "").Replace("-", "");
                _mdnsService = new MdnsService(
                    mdnsLogger,
                    mdnsName,
                    _port,
                    "PlateReaderService", "SiLA2Common"
                );
                await _mdnsService.RegisterAsync();
                _logger.LogInformation("[mDNS] Server registrato per auto-discovery");
            }
            catch (Exception ex)
            {
                _logger.LogWarning("[mDNS] Registrazione fallita (non critico): {Message}", ex.Message);
            }
        }

        public async Task StopAsync(CancellationToken cancellationToken)
        {
            _logger.LogInformation("Shutting down gRPC Server...");
            
            // Rimuovi registrazione mDNS
            if (_mdnsService != null)
            {
                try
                {
                    await _mdnsService.UnregisterAsync();
                    _mdnsService.Dispose();
                }
                catch
                {
                    // Ignora errori durante shutdown
                }
            }

            if (_server != null)
            {
                await _server.ShutdownAsync();
            }
        }
    }
}
